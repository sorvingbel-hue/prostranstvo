// TEST HARNESS ONLY. Compile separately with PetTricks.cs, not the desktop pet.
// Requires a new isolated directory with .fixture-owned containing exactly
// prostranstvo-pet-tricks-fixture-v1. It never opens a desktop window or audio.
using System;
using System.Diagnostics;
using System.IO;
using System.Threading;
namespace ProstranstvoPet {
 static class PetTricksFixture {
  static int Main(string[] args) {
   try {
    if(args.Length==1&&args[0]=="--self-test") {int count=PetTrickTests.Run();Console.WriteLine("{\"status\":\"self_test_ok\",\"trick_checks\":"+count+",\"audio_played\":false,\"desktop_mutations\":false}");return 0;}
    if(args.Length!=1||!Path.IsPathRooted(args[0]))return 2;string root=Path.GetFullPath(args[0]);TrickFiles.NoLinks(root);
    string marker=TrickFiles.Read(Path.Combine(root,".fixture-owned"),128);
    if(root==Path.GetFullPath(TrickFiles.DataRoot())||!Directory.Exists(root)||marker==null||marker.Trim()!="prostranstvo-pet-tricks-fixture-v1")return 2;
    TricksHost host=new TricksHost(root,Path.Combine(root,"fixture-study.json"));Stopwatch clock=Stopwatch.StartNew();
    Console.WriteLine("{\"status\":\"fixture_ready\",\"audio_played\":false,\"desktop_mutations\":false,\"network\":false}");
    while(clock.Elapsed.TotalSeconds<60){host.Advance(clock.Elapsed.TotalSeconds,TrickFiles.UtcNow(),false);Thread.Sleep(40);}
    host.Stop(TrickFiles.UtcNow());Console.WriteLine("{\"status\":\"fixture_finished\"}");return 0;
   }catch{Console.Error.WriteLine("fixture_failed");return 1;}
  }
 }
}
