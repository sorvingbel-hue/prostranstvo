using System;
using System.Drawing;
using System.Drawing.Drawing2D;

namespace ProstranstvoPet {
 enum PetDayPart { Morning, Day, Evening, Night, Unknown }
 enum PetSeason { Spring, Summer, Autumn, Winter, Unknown }
 enum PetWeather { Fair, Cloud, Rain, Snow }

 sealed class AmbienceSnapshot {
  public readonly PetDayPart Part;
  public readonly PetSeason Season;
  public readonly PetWeather Weather;
  public readonly float Daylight;
  public readonly bool Available,NightRest;
  public readonly string MinuteKey;
  public AmbienceSnapshot(PetDayPart part,PetSeason season,PetWeather weather,float light,bool available,string key) {
   Part=part;Season=season;Weather=weather;Daylight=light;Available=available;MinuteKey=key;
   NightRest=part==PetDayPart.Night||!available;
  }
  public string PartLabel {get {return Part==PetDayPart.Morning?"Утро":Part==PetDayPart.Day?"День":Part==PetDayPart.Evening?"Вечер":Part==PetDayPart.Night?"Ночь":"Время не определено";}}
  public string SeasonLabel {get {return Season==PetSeason.Spring?"Весна":Season==PetSeason.Summer?"Лето":Season==PetSeason.Autumn?"Осень":Season==PetSeason.Winter?"Зима":"Сезон не определён";}}
  public string WeatherLabel {get {return Weather==PetWeather.Fair?"ясно":Weather==PetWeather.Cloud?"облачно":Weather==PetWeather.Rain?"дождик":"снежок";}}
  public string Description {get {return PartLabel+" · "+SeasonLabel+" · сказочная погода: "+WeatherLabel;}}
 }

 // A miniature fictional world: local wall clock and calendar only. No location,
 // forecast, network or information about the child's activity is requested.
 static class PetAmbience {
  static float Clamp(float value){return Math.Max(0,Math.Min(1,value));}
  public static AmbienceSnapshot Unknown() {return new AmbienceSnapshot(PetDayPart.Unknown,PetSeason.Unknown,PetWeather.Cloud,0,false,"unknown");}
  public static AmbienceSnapshot ReadClock(Func<DateTime> clock) {
   try {return FromLocal(clock());}catch{return Unknown();}
  }
  public static bool ShouldRender(bool enabled,bool fullscreen,bool quiet,bool available){return enabled&&!fullscreen&&!quiet&&available;}
  public static AmbienceSnapshot FromLocal(DateTime local) {
   if(local==DateTime.MinValue||local==DateTime.MaxValue)return Unknown();
   int hour=local.Hour,month=local.Month;
   PetDayPart part=hour>=5&&hour<10?PetDayPart.Morning:hour>=10&&hour<17?PetDayPart.Day:hour>=17&&hour<21?PetDayPart.Evening:PetDayPart.Night;
   // Explicit northern-hemisphere calendar seasons, not geographic observation.
   PetSeason season=month>=3&&month<=5?PetSeason.Spring:month>=6&&month<=8?PetSeason.Summer:month>=9&&month<=11?PetSeason.Autumn:PetSeason.Winter;
   int seed=unchecked(local.Year*397+local.DayOfYear*31+(hour/6)*17)&0x7fffffff;
   int choice=seed%10;
   PetWeather weather=choice<5?PetWeather.Fair:choice<8?PetWeather.Cloud:season==PetSeason.Winter?PetWeather.Snow:PetWeather.Rain;
   double hours=hour+local.Minute/60.0;
   float light=Clamp((float)((Math.Cos((hours-12)*Math.PI/12)+.10)/1.10));
   return new AmbienceSnapshot(part,season,weather,light,true,local.ToString("yyyyMMddHHmm",System.Globalization.CultureInfo.InvariantCulture));
  }
  static Color Alpha(int value,Color color){return Color.FromArgb(Math.Max(0,Math.Min(255,value)),color);}
  public static void Draw(Graphics g,AmbienceSnapshot snapshot,double animation) {
   if(snapshot==null||!snapshot.Available)return;
   if(Double.IsNaN(animation)||Double.IsInfinity(animation)||animation<0)animation=0;
   GraphicsState saved=g.Save();
   try {
    g.SmoothingMode=SmoothingMode.AntiAlias;
    Color tint=snapshot.Season==PetSeason.Winter?Color.FromArgb(177,207,237):snapshot.Season==PetSeason.Spring?Color.FromArgb(186,221,181):snapshot.Season==PetSeason.Autumn?Color.FromArgb(241,195,143):Color.FromArgb(247,223,157);
    using(Brush glow=new SolidBrush(Alpha(28,tint)))g.FillEllipse(glow,3,2,133,87);
    int sunAlpha=(int)(180*snapshot.Daylight);
    if(sunAlpha>0) {
     using(Brush halo=new SolidBrush(Color.FromArgb(sunAlpha/4,255,220,135)))g.FillEllipse(halo,2,1,42,42);
     using(Brush sun=new SolidBrush(Color.FromArgb(sunAlpha,255,218,125)))g.FillEllipse(sun,13,12,20,20);
     using(Pen ray=new Pen(Color.FromArgb(sunAlpha/2,240,184,80),1.3f)) {
      for(int i=0;i<8;i++){double angle=i*Math.PI/4+Math.Sin(animation*.15)*.08;g.DrawLine(ray,23+(float)Math.Cos(angle)*14,22+(float)Math.Sin(angle)*14,23+(float)Math.Cos(angle)*17,22+(float)Math.Sin(angle)*17);}
     }
    }
    int moonAlpha=(int)(195*(1-snapshot.Daylight));
    if(moonAlpha>0) {
     using(GraphicsPath outer=new GraphicsPath())using(GraphicsPath cut=new GraphicsPath()) {
      outer.AddEllipse(13,9,20,22);cut.AddEllipse(19,5,18,21);
      using(Region crescent=new Region(outer)){crescent.Exclude(cut);using(Brush moon=new SolidBrush(Color.FromArgb(moonAlpha,238,220,170)))g.FillRegion(moon,crescent);}
     }
     using(Brush star=new SolidBrush(Color.FromArgb(moonAlpha*2/3,231,227,250))) {
      g.FillEllipse(star,4,12,2,2);g.FillEllipse(star,36,36,2,2);g.FillEllipse(star,9,40,1.5f,1.5f);
     }
    }
    if(snapshot.Weather!=PetWeather.Fair) {
     float drift=(float)Math.Sin(animation*.22)*2;
     Color cloudColor=snapshot.Part==PetDayPart.Night?Color.FromArgb(115,167,179,209):Color.FromArgb(148,229,234,239);
     using(Brush cloud=new SolidBrush(cloudColor)) {
      g.FillEllipse(cloud,91+drift,22,27,12);g.FillEllipse(cloud,99+drift,15,18,19);g.FillEllipse(cloud,111+drift,23,19,11);
     }
    }
    if(snapshot.Weather==PetWeather.Rain||snapshot.Weather==PetWeather.Snow) {
     for(int i=0;i<5;i++) {
      float progress=(float)((animation*(snapshot.Weather==PetWeather.Snow?.11:.21)+i*.21)%1);
      float x=(i%2==0?8:126)+(float)Math.Sin(animation*.6+i)*3;
      float y=38+progress*76;
      int opacity=(int)(105*Math.Sin(progress*Math.PI));
      if(snapshot.Weather==PetWeather.Snow)using(Brush snow=new SolidBrush(Color.FromArgb(opacity,225,236,250)))g.FillEllipse(snow,x,y,3,3);
      else using(Pen rain=new Pen(Color.FromArgb(opacity,143,185,218),1.1f))g.DrawLine(rain,x,y,x-1.5f,y+4);
     }
    }
    if(snapshot.Season==PetSeason.Autumn) {
     float sway=(float)Math.Sin(animation*.4)*3;
     using(Brush leaf=new SolidBrush(Color.FromArgb(125,218,158,100))) {
      g.FillEllipse(leaf,4+sway,119,7,3);g.FillEllipse(leaf,125-sway,110,6,3);
     }
    } else if(snapshot.Season==PetSeason.Spring) {
     using(Brush flower=new SolidBrush(Color.FromArgb(115,229,171,190)))g.FillEllipse(flower,8,119,4,4);
    }
   } finally {g.Restore(saved);}
  }
  public static Bitmap Build(AmbienceSnapshot snapshot,double animation) {
   Bitmap image=new Bitmap(Painter.Width,Painter.Height, System.Drawing.Imaging.PixelFormat.Format32bppPArgb);
   using(Graphics g=Graphics.FromImage(image)){g.Clear(Color.Transparent);Draw(g,snapshot,animation);}
   return image;
  }
  public static int SelfTest() {
   int checks=0;Action<bool> check=delegate(bool value){checks++;if(!value)throw new Exception("ambience_self_test_failed");};
   int[] hours={4,5,9,10,16,17,20,21,23,0};
   PetDayPart[] parts={PetDayPart.Night,PetDayPart.Morning,PetDayPart.Morning,PetDayPart.Day,PetDayPart.Day,PetDayPart.Evening,PetDayPart.Evening,PetDayPart.Night,PetDayPart.Night,PetDayPart.Night};
   for(int i=0;i<hours.Length;i++)check(FromLocal(new DateTime(2026,5,10,hours[i],0,0)).Part==parts[i]);
   PetSeason[] seasons={PetSeason.Winter,PetSeason.Winter,PetSeason.Spring,PetSeason.Spring,PetSeason.Spring,PetSeason.Summer,PetSeason.Summer,PetSeason.Summer,PetSeason.Autumn,PetSeason.Autumn,PetSeason.Autumn,PetSeason.Winter};
   for(int month=1;month<=12;month++)check(FromLocal(new DateTime(2026,month,12,12,0,0)).Season==seasons[month-1]);
   check(FromLocal(new DateTime(2026,1,1,0,0,0)).NightRest&&!FromLocal(new DateTime(2026,1,1,12,0,0)).NightRest);
   check(!FromLocal(DateTime.MinValue).Available&&!FromLocal(DateTime.MaxValue).Available);
   check(!ReadClock(delegate{throw new InvalidOperationException();}).Available);
   check(!ReadClock(null).Available&&Unknown().NightRest);
   AmbienceSnapshot repeated=FromLocal(new DateTime(2026,11,6,14,20,0));
   check(repeated.Description==FromLocal(new DateTime(2026,11,6,14,20,0)).Description);
   check(repeated.Weather==FromLocal(new DateTime(2026,11,6,14,59,0)).Weather);
   check(FromLocal(new DateTime(2026,11,1,1,30,0)).Description==FromLocal(new DateTime(2026,11,1,1,30,0)).Description);
   for(int hour=0;hour<24;hour++){float light=FromLocal(new DateTime(2026,4,1,hour,30,0)).Daylight;check(!Single.IsNaN(light)&&light>=0&&light<=1);}
   check(FromLocal(new DateTime(2026,4,1,12,0,0)).Daylight>FromLocal(new DateTime(2026,4,1,0,0,0)).Daylight);
   bool snow=false,rain=false;
   for(int day=0;day<365;day++) {
    AmbienceSnapshot value=FromLocal(new DateTime(2026,1,1,12,0,0).AddDays(day));
    if(value.Weather==PetWeather.Snow){snow=true;check(value.Season==PetSeason.Winter);}
    if(value.Weather==PetWeather.Rain){rain=true;check(value.Season!=PetSeason.Winter);}
   }
   check(snow&&rain);
   check(ShouldRender(true,false,false,true));check(!ShouldRender(false,false,false,true));check(!ShouldRender(true,true,false,true));check(!ShouldRender(true,false,true,true));check(!ShouldRender(true,false,false,false));
   using(Bitmap sky=Build(repeated,.5)) {
    check(sky.Width==Painter.Width&&sky.Height==Painter.Height);
    check(sky.GetPixel(139,162).A==0&&sky.GetPixel(70,150).A==0);
   }
   using(Bitmap absent=Build(Unknown(),Double.NaN)){check(absent.GetPixel(23,22).A==0);}
   return checks;
  }
 }
}
