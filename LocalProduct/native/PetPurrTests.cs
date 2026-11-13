using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using ProstranstvoPet;

sealed class FakePurrOutput : IPurrOutput {
 public int Plays,Stops,Disposals;public bool FailPlay,FailStop;
 public void PlayLooping(){Plays++;if(FailPlay)throw new IOException("fixture");}
 public void Stop(){Stops++;if(FailStop)throw new IOException("fixture");}
 public void Dispose(){Disposals++;}
}
static class PetPurrTests {
 static int checks;
 static void Check(bool value,string name){checks++;if(!value)throw new Exception("purr_test_failed: "+name);}
 static int Int(byte[] bytes,int at){return BitConverter.ToInt32(bytes,at);}
 static short Short(byte[] bytes,int at){return BitConverter.ToInt16(bytes,at);}
 static void Wave() {
  byte[] wave=PurrPlayer.GenerateWaveBytes(),again=PurrPlayer.GenerateWaveBytes();
  Check(wave.Length==44+PurrPlayer.SampleRate*PurrPlayer.DurationSeconds*2,"bounded duration");
  Check(Encoding.ASCII.GetString(wave,0,4)=="RIFF"&&Encoding.ASCII.GetString(wave,8,8)=="WAVEfmt ","wave header");
  Check(Int(wave,4)==wave.Length-8&&Int(wave,16)==16&&Short(wave,20)==1,"PCM header sizes");
  Check(Short(wave,22)==1&&Int(wave,24)==22050&&Int(wave,28)==44100&&Short(wave,32)==2&&Short(wave,34)==16,"PCM format");
  Check(Encoding.ASCII.GetString(wave,36,4)=="data"&&Int(wave,40)==wave.Length-44,"data chunk");
  bool equal=wave.Length==again.Length;for(int i=0;i<wave.Length&&equal;i++)equal=wave[i]==again[i];Check(equal,"deterministic");
  double energy=0,mean=0;int peak=0,largestDelta=0,last=0;
  int samples=(wave.Length-44)/2;
  for(int i=0;i<samples;i++) {
   int sample=Short(wave,44+i*2);peak=Math.Max(peak,Math.Abs(sample));
   energy+=(double)sample*sample;mean+=sample;
   if(i>0)largestDelta=Math.Max(largestDelta,Math.Abs(sample-last));last=sample;
  }
  double rms=Math.Sqrt(energy/samples);
  Check(peak>1000&&peak<7500,"quiet unclipped peak");
  Check(rms>400&&rms<2500,"audible soft energy");
  Check(Math.Abs(mean/samples)<1,"no DC offset");
  Check(Math.Abs(Short(wave,44)-Short(wave,wave.Length-2))<4,"seam amplitude");
  Check(Math.Abs(Short(wave,46)-Short(wave,44))<4&&Math.Abs(Short(wave,wave.Length-2)-Short(wave,wave.Length-4))<4,"soft boundary derivative");
  Check(largestDelta<1000,"no sample discontinuity");
 }
 static void Lifecycle() {
  Queue<Action> work=new Queue<Action>();FakePurrOutput output=new FakePurrOutput();int factories=0;
  using(PurrPlayer player=new PurrPlayer(delegate(byte[] bytes){factories++;return output;},delegate(Action action){work.Enqueue(action);})) {
   Check(factories==0&&work.Count==0&&output.Plays==0,"constructor inert");
   for(int i=0;i<100;i++)player.SetActive(true);
   Check(work.Count==1&&output.Plays==0,"single asynchronous prepare");
   work.Dequeue()();Check(factories==1&&output.Plays==1&&player.Available&&player.IsPlaying,"play when ready");
   for(int i=0;i<100;i++)player.SetActive(true);Check(output.Plays==1,"active idempotent at 30FPS");
   player.SetActive(false);Check(output.Stops==1&&!player.IsPlaying,"stop immediately");
   for(int i=0;i<100;i++)player.SetActive(false);Check(output.Stops==1,"inactive idempotent");
   player.SetActive(true);Check(output.Plays==2&&factories==1,"cached replay");
   player.Enabled=false;Check(!player.IsPlaying&&output.Stops==2,"mute stops");
   player.SetActive(true);Check(output.Plays==2,"muted does not play");
   player.Enabled=true;Check(output.Plays==2,"unmute does not start by itself");
   player.SetActive(true);Check(output.Plays==3,"new deliberate active resumes");
   player.Stop();Check(!player.IsPlaying&&output.Stops==3,"explicit stop");
  }
  Check(output.Disposals==1,"dispose output once");
  Queue<Action> pending=new Queue<Action>();FakePurrOutput late=new FakePurrOutput();
  PurrPlayer cancelled=new PurrPlayer(delegate(byte[] bytes){return late;},delegate(Action action){pending.Enqueue(action);});
  cancelled.SetActive(true);cancelled.Stop();pending.Dequeue()();
  Check(late.Plays==0&&!cancelled.IsPlaying,"no late sound after stop during loading");
  cancelled.SetActive(true);Check(late.Plays==1,"loaded silent sound can resume");
  cancelled.Dispose();cancelled.Dispose();cancelled.SetActive(true);cancelled.Enabled=true;
  Check(late.Disposals==1&&late.Plays==1&&!cancelled.Available,"dispose idempotent ignores later calls");
  FakePurrOutput orphan=new FakePurrOutput();
  PurrPlayer closed=new PurrPlayer(delegate(byte[] bytes){return orphan;},delegate(Action action){pending.Enqueue(action);});
  closed.SetActive(true);closed.Dispose();pending.Dequeue()();
  Check(orphan.Plays==0&&orphan.Disposals==1,"disposed while loading never plays");
  PurrPlayer failed=new PurrPlayer(delegate(byte[] bytes){throw new IOException("fixture private error");},delegate(Action action){pending.Enqueue(action);});
  failed.SetActive(true);pending.Dequeue()();failed.SetActive(true);failed.Stop();failed.Dispose();
  Check(pending.Count==0&&!failed.IsPlaying&&!failed.Available,"output load error fails silent");
  FakePurrOutput bad=new FakePurrOutput();bad.FailPlay=true;
  using(PurrPlayer player=new PurrPlayer(delegate(byte[] bytes){return bad;},delegate(Action action){pending.Enqueue(action);})) {
   player.SetActive(true);pending.Dequeue()();Check(!player.IsPlaying&&!player.Available&&bad.Disposals==1,"play error fails silent");
  }
  FakePurrOutput badStop=new FakePurrOutput();
  using(PurrPlayer player=new PurrPlayer(delegate(byte[] bytes){return badStop;},delegate(Action action){pending.Enqueue(action);})) {
   player.SetActive(true);pending.Dequeue()();badStop.FailStop=true;player.Stop();
   Check(!player.IsPlaying&&!player.Available&&badStop.Disposals==1,"stop error fails silent");
  }
 }
 static int Main() {
  try {Wave();Lifecycle();Console.WriteLine("{\"status\":\"purr_self_test_ok\",\"checks\":"+checks+",\"audio_played\":false,\"network\":false}");return 0;}
  catch(Exception error){Console.Error.WriteLine(error.Message);return 1;}
 }
}
