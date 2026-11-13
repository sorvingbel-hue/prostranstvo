using System;
using System.IO;
using System.Media;
using System.Text;
using System.Threading;

namespace ProstranstvoPet {
 // A quiet, original synthesized sound. No network, recording, volume or routing changes.
 interface IPurrOutput : IDisposable {
  void PlayLooping();
  void Stop();
 }

 sealed class WavePurrOutput : IPurrOutput {
  readonly MemoryStream stream;
  readonly SoundPlayer player;
  public WavePurrOutput(byte[] wave) {
   stream=new MemoryStream(wave,false);
   player=new SoundPlayer(stream);
   try {player.Load();} catch {player.Dispose();stream.Dispose();throw;}
  }
  public void PlayLooping() {player.PlayLooping();}
  public void Stop() {player.Stop();}
  public void Dispose() {player.Dispose();stream.Dispose();}
 }

 sealed class PurrPlayer : IDisposable {
  public const int SampleRate=22050;
  public const int DurationSeconds=4;
  static readonly Lazy<byte[]> CachedWave=new Lazy<byte[]>(GenerateWaveBytes,true);
  readonly object gate=new object();
  readonly Func<byte[],IPurrOutput> factory;
  readonly Action<Action> schedule;
  IPurrOutput output;
  bool enabled=true,requested,initializing,playing,disposed,failed;

  // Construction is inert: first intentional SetActive(true) prepares audio off the UI thread.
  public PurrPlayer() : this(delegate(byte[] wave){return new WavePurrOutput(wave);},
   delegate(Action action){ThreadPool.QueueUserWorkItem(delegate{action();});}) { }

  internal PurrPlayer(Func<byte[],IPurrOutput> outputFactory,Action<Action> scheduler) {
   if(outputFactory==null||scheduler==null)throw new ArgumentNullException();
   factory=outputFactory;schedule=scheduler;
  }

  public bool Enabled {
   get {lock(gate)return enabled&&!disposed;}
   set {
    lock(gate) {
     if(disposed)return;
     enabled=value;
     if(!value){requested=false;StopLocked();}
    }
   }
  }
  // True means the local output was prepared, not proof that a speaker is audible.
  public bool Available {get {lock(gate)return output!=null&&!failed&&!disposed;}}
  public bool IsPlaying {get {lock(gate)return playing&&!disposed;}}

  public void SetActive(bool active) {
   lock(gate) {
    if(disposed)return;
    requested=active&&enabled&&!failed;
    if(!requested){StopLocked();return;}
    if(output!=null){PlayLocked();return;}
    if(initializing)return;
    initializing=true;
    try {schedule(Initialize);} catch {initializing=false;failed=true;requested=false;}
   }
  }

  void Initialize() {
   IPurrOutput prepared=null;
   try {prepared=factory(CachedWave.Value);if(prepared==null)throw new InvalidOperationException();}
   catch {
    lock(gate){initializing=false;failed=true;requested=false;}
    if(prepared!=null)SafeDispose(prepared);
    return;
   }
   lock(gate) {
    initializing=false;
    if(disposed||failed){SafeDispose(prepared);return;}
    output=prepared;
    if(requested&&enabled)PlayLocked();
   }
  }

  void PlayLocked() {
   if(playing||output==null)return;
   try {output.PlayLooping();playing=true;}
   catch {failed=true;requested=false;playing=false;SafeDispose(output);output=null;}
  }
  void StopLocked() {
   if(!playing)return;
   playing=false;
   if(output!=null) {
    try {output.Stop();}
    catch {failed=true;SafeDispose(output);output=null;}
   }
  }
  public void Stop() {lock(gate){requested=false;StopLocked();}}

  static void SafeDispose(IPurrOutput value) {
   try {value.Stop();} catch { }
   try {value.Dispose();} catch { }
  }
  public void Dispose() {
   lock(gate) {
    if(disposed)return;
    disposed=true;requested=false;StopLocked();
    if(output!=null){SafeDispose(output);output=null;}
   }
  }

  // PCM16 mono, warm 25 Hz pulses with quiet textured harmonics and a slow breath.
  // Every oscillator is periodic over this four-second loop; the boundary also
  // has a short raised-sine envelope to soften the first edge and loop seam.
  internal static byte[] GenerateWaveBytes() {
   int samples=SampleRate*DurationSeconds;
   using(MemoryStream bytes=new MemoryStream(44+samples*2)) {
    using(BinaryWriter writer=new BinaryWriter(bytes,Encoding.ASCII,true)) {
     writer.Write(Encoding.ASCII.GetBytes("RIFF"));writer.Write(36+samples*2);
     writer.Write(Encoding.ASCII.GetBytes("WAVEfmt "));writer.Write(16);
     writer.Write((short)1);writer.Write((short)1);writer.Write(SampleRate);
     writer.Write(SampleRate*2);writer.Write((short)2);writer.Write((short)16);
     writer.Write(Encoding.ASCII.GetBytes("data"));writer.Write(samples*2);
     double tau=Math.PI*2;
     int[] textureHz={83,109,137,163,197,229,269,307,347,397,443,503};
     for(int i=0;i<samples;i++) {
      double t=(double)i/SampleRate;
      double pulse=Math.Pow(.5+.5*Math.Cos(tau*25*t),1.8);
      double warm=.42*Math.Sin(tau*75*t)+.25*Math.Sin(tau*100*t)+
                  .14*Math.Sin(tau*150*t)+.08*Math.Sin(tau*200*t);
      double air=0;
      for(int harmonic=0;harmonic<textureHz.Length;harmonic++)
       air+=(harmonic%2==0?1:-1)*Math.Sin(tau*textureHz[harmonic]*t)/textureHz.Length;
      double breath=.68+.20*Math.Cos(tau*.5*t)+.07*Math.Cos(tau*t);
      double edge=Math.Min(1,Math.Min(t,DurationSeconds-t)/.025);
      edge=Math.Pow(Math.Sin(edge*Math.PI/2),2);
      double value=.15*edge*breath*(.55*Math.Sin(tau*25*t)+
                   .70*(.28+.72*pulse)*warm+.14*(.2+.8*pulse)*air);
      value=Math.Max(-.22,Math.Min(.22,value));
      writer.Write((short)Math.Round(value*32767));
     }
    }
    return bytes.ToArray();
   }
  }
 }
}
