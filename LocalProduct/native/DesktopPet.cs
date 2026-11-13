using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Windows.Forms;

// Ordinary visible desktop toy. No keyboard hooks, title/content reads or commands.
namespace ProstranstvoPet {
 enum PetMode { Roam, Calm, Quiet }
 enum PetStage { Idle, Walk, Approach, ClimbUp, Perch, ClimbDown, Park }
 sealed class Surface {
  public long Id; public Rectangle Bounds;
  public Surface(long id, Rectangle bounds) { Id=id; Bounds=bounds; }
 }
 // Only local body mouse events enter this model. Screen displacement avoids
 // treating a kitten walking beneath a stationary pointer as a child's stroke.
 sealed class Petting {
  Point previous;double sampled=-1,activeUntil;float travel;int segments;
  public bool Hover { get; private set; }
  public void Cancel(){Hover=false;sampled=-1;activeUntil=0;travel=0;segments=0;}
  public bool Active(double now){return Hover&&now>=sampled&&now<activeUntil;}
  public float Strength(double now){return Active(now)?(float)Math.Min(1,Math.Max(0,(activeUntil-now)/.45)):0;}
  public bool Sample(Point screen,bool body,bool blocked,double now) {
   if(!body||blocked||Double.IsNaN(now)||Double.IsInfinity(now)||now<0){Cancel();return false;}
   if(!Hover||now<sampled||now-sampled>.45){Hover=true;previous=screen;sampled=now;travel=0;segments=0;activeUntil=0;return false;}
   double dx=(double)screen.X-previous.X,dy=(double)screen.Y-previous.Y;
   previous=screen;sampled=now;double distance=Math.Sqrt(dx*dx+dy*dy);
   if(distance<.5)return false;
   if(distance>85){travel=0;segments=0;activeUntil=0;return false;}
   travel+=(float)Math.Min(distance,25);segments=Math.Min(2,segments+1);
   if(travel<14||segments<2)return false;
   activeUntil=now+.85;travel=Math.Min(8,Math.Max(0,travel-12));return true;
  }
 }

 sealed class PettingCursors : IDisposable {
  readonly IntPtr[] handles=new IntPtr[6];bool disposed;
  public int OwnedCount { get {int count=0;foreach(IntPtr value in handles)if(value!=IntPtr.Zero)count++;return count;} }
  public bool Owns(IntPtr value){foreach(IntPtr handle in handles)if(handle!=IntPtr.Zero&&handle==value)return true;return false;}
  public IntPtr Frame(double now,bool stroking) {
   if(disposed)return Cursors.Hand.Handle;
   int index=stroking?(int)((now% .72)/.12):2;
   index=Math.Max(0,Math.Min(5,index));
   if(handles[index]==IntPtr.Zero) {
    IntPtr color=IntPtr.Zero,mask=IntPtr.Zero;
    try {
     using(Bitmap image=Image(index))using(Bitmap monochrome=new Bitmap(40,40,PixelFormat.Format1bppIndexed)) {
      color=image.GetHbitmap(Color.FromArgb(0));mask=monochrome.GetHbitmap();
      Native.ICONINFO info=new Native.ICONINFO();info.icon=false;info.hotX=20;info.hotY=18;info.color=color;info.mask=mask;
      handles[index]=Native.CreateIconIndirect(ref info);
     }
    } catch { }
    finally {if(color!=IntPtr.Zero)Native.DeleteObject(color);if(mask!=IntPtr.Zero)Native.DeleteObject(mask);}
   }
   return handles[index]!=IntPtr.Zero?handles[index]:Cursors.Hand.Handle;
  }
  public static Bitmap Image(int phase) {
   Bitmap image=new Bitmap(40,40,PixelFormat.Format32bppPArgb);
   using(Graphics g=Graphics.FromImage(image))Paint(g,phase);
   return image;
  }
  public static void Paint(Graphics g,int phase) {
   GraphicsState saved=g.Save();
   try {
    g.SmoothingMode=SmoothingMode.AntiAlias;
    g.TranslateTransform(20,20);g.RotateTransform((phase-2.5f)*3);g.TranslateTransform(-20,-20);
    Color skin=Color.FromArgb(255,239,200,171),edge=Color.FromArgb(238,164,111,83);
    using(Pen outline=new Pen(edge,6))using(Pen finger=new Pen(skin,3.8f)) {
     outline.StartCap=outline.EndCap=finger.StartCap=finger.EndCap=LineCap.Round;
     PointF[] bottom={new PointF(14,20),new PointF(20,18),new PointF(26,19),new PointF(31,22),new PointF(14,25)};
     PointF[] top={new PointF(13,8),new PointF(20,4),new PointF(26,7),new PointF(32,13),new PointF(6,18)};
     for(int i=0;i<bottom.Length;i++){g.DrawLine(outline,bottom[i],top[i]);g.DrawLine(finger,bottom[i],top[i]);}
    }
    using(Brush fill=new SolidBrush(skin))using(Pen outline=new Pen(edge,1.2f)) {
     PointF[] wrist={new PointF(15,26),new PointF(29,26),new PointF(28,38),new PointF(17,38)};
     g.FillPolygon(fill,wrist);g.DrawLines(outline,new PointF[]{wrist[3],wrist[0],wrist[1],wrist[2]});
     g.FillEllipse(fill,11,14,23,18);g.DrawArc(outline,11,14,23,18,7,164);
     using(Pen crease=new Pen(Color.FromArgb(180,198,142,111),1)){g.DrawArc(crease,16,19,13,9,198,97);g.DrawArc(crease,17,21,10,7,213,80);}
    }
   } finally {g.Restore(saved);}
  }
  public void Dispose() {
   if(disposed)return;disposed=true;
   if(Owns(Native.GetCursor()))Native.SetCursor(Cursors.Arrow.Handle);
   for(int i=0;i<handles.Length;i++)if(handles[i]!=IntPtr.Zero){Native.DestroyCursor(handles[i]);handles[i]=IntPtr.Zero;}
  }
 }
 sealed class Motion {
  readonly Random random;
  public Rectangle Area;
  public float X,Y,TargetX,TargetY;
  public int Width {get;private set;} public int Height {get;private set;}
  public PetMode Mode=PetMode.Roam;
  public PetStage Stage=PetStage.Idle;
  public bool FacingLeft;
  public bool Fullscreen;
  public bool NightRest;
  public float Wait=2;
  public long PerchId;
  public Motion(Rectangle area,int width,int height,int seed) {
   Area=area; Width=width; Height=height; random=new Random(seed);
   X=Right-20; Y=Floor; TargetX=X;TargetY=Y;ClampPosition();
  }
  public float Left { get { return Area.Left+3; } }
  public float Right { get { return Math.Max(Left,Area.Right-Width-3); } }
  public float Floor { get { return Math.Max(Area.Top,Area.Bottom-Height); } }
  float ClampX(float value) { return Math.Max(Left,Math.Min(Right,value)); }
  float ClampY(float value) { return Math.Max(Area.Top,Math.Min(Floor,value)); }
  public void ClampPosition() { X=ClampX(X);Y=ClampY(Y);TargetX=ClampX(TargetX);TargetY=ClampY(TargetY); }
  public void SetArea(Rectangle value) { Area=value;ClampPosition(); }
  public void SetSize(int width,int height) {
   if(width<1||height<1||width>1024||height>1200)throw new ArgumentOutOfRangeException();
   float center=X+Width/2f,bottom=Y+Height;
   Width=width;Height=height;X=TargetX=center-width/2f;Y=TargetY=bottom-height;
   Stage=PetStage.Idle;PerchId=0;Wait=3;ClampPosition();
   if(Mode==PetMode.Quiet||Fullscreen)Park();
  }
  public void Place(float x,float y) { X=TargetX=ClampX(x);Y=TargetY=ClampY(y);Stage=PetStage.Idle;PerchId=0;Wait=4; }
  public void SetMode(PetMode mode) {
   Mode=mode;PerchId=0;Wait=3;
   if(mode==PetMode.Quiet) Park();
   else { Stage=PetStage.Idle;TargetX=X;TargetY=Y; }
  }
  void Park() { Stage=PetStage.Park;PerchId=0;TargetX=Right;TargetY=Floor; }
  public static bool IsFullscreen(Rectangle screen,Rectangle window,bool visible,bool own) {
   return visible&&!own&&window.Width>0&&window.Height>0&&window.Left<=screen.Left+2&&window.Top<=screen.Top+2&&window.Right>=screen.Right-2&&window.Bottom>=screen.Bottom-2;
  }
  public static int MouseStyle(int style,bool fullscreen) { return fullscreen?style|0x20:style&~0x20; }
  public static bool UsableSurface(Rectangle value,Rectangle area,int petHeight) {
   return value.Width>=300&&value.Height>=150&&value.Top>=area.Top+petHeight+30&&value.Top<area.Bottom-100&&value.Left<area.Right-80&&value.Right>area.Left+80;
  }
  public bool PlanPerch(Surface surface) {
   if(!UsableSurface(surface.Bounds,Area,Height))return false;
   PerchId=surface.Id;Stage=PetStage.Approach;
   TargetX=ClampX(surface.Bounds.Left-Width*.55f);TargetY=Floor;return true;
  }
  void PerchTarget(Surface surface) { TargetX=ClampX(surface.Bounds.Left-Width*.55f);TargetY=ClampY(surface.Bounds.Top-Height+22*Width/140f); }
  public void Step(float elapsed,IList<Surface> surfaces,bool fullscreen) {
   if(Single.IsNaN(elapsed)||Single.IsInfinity(elapsed)||elapsed<=0)return;
   float dt=Math.Min(.06f,elapsed);
   if(fullscreen&&!Fullscreen)Park();
   if(!fullscreen&&Fullscreen) { Stage=PetStage.Idle;TargetX=X;TargetY=Y;Wait=3; }
   Fullscreen=fullscreen;
   if(fullscreen||Mode==PetMode.Quiet) {
    if(Stage!=PetStage.Park)Park();
   } else if(Mode==PetMode.Calm) { TargetX=X;TargetY=Y;Stage=PetStage.Idle;return; }
   Surface current=null;
   if(PerchId!=0)foreach(Surface surface in surfaces)if(surface.Id==PerchId&&UsableSurface(surface.Bounds,Area,Height)){current=surface;break;}
   if(PerchId!=0&&current==null) { PerchId=0;Stage=PetStage.ClimbDown;TargetX=X;TargetY=Floor; }
   if((Stage==PetStage.ClimbUp||Stage==PetStage.Perch)&&current!=null) {
    PerchTarget(current);
    if(Stage==PetStage.Perch&&(Math.Abs(X-TargetX)>1||Math.Abs(Y-TargetY)>1))Stage=PetStage.ClimbUp;
   }
   if(Stage==PetStage.Idle||Stage==PetStage.Perch) {
    Wait-=dt;
    if(Wait<=0) {
     if(Stage==PetStage.Perch) { Stage=PetStage.ClimbDown;TargetX=X;TargetY=Floor;PerchId=0; }
     else if(NightRest&&random.Next(4)!=0) { Stage=PetStage.Idle;TargetX=X;TargetY=Y;Wait=12+(float)random.NextDouble()*12; }
     else if(surfaces.Count>0&&random.Next(4)==0&&PlanPerch(surfaces[random.Next(surfaces.Count)])) { }
     else { Stage=PetStage.Walk;TargetX=Left+(float)random.NextDouble()*(Right-Left);TargetY=Floor; }
    }
   }
   float dx=TargetX-X,dy=TargetY-Y;
   if(Math.Abs(dx)>1)FacingLeft=dx<0;
   float distance=(float)Math.Sqrt(dx*dx+dy*dy);
   float speed=(Stage==PetStage.ClimbUp||Stage==PetStage.ClimbDown)?35:43;
   if(Stage==PetStage.Park)speed=100;
   if(distance>0) { float amount=Math.Min(distance,speed*dt);X+=dx/distance*amount;Y+=dy/distance*amount; }
   if(distance<=speed*dt+.1f) {
    X=TargetX;Y=TargetY;
    if(Stage==PetStage.Approach&&current!=null) { Stage=PetStage.ClimbUp;PerchTarget(current); }
    else if(Stage==PetStage.ClimbUp) { Stage=PetStage.Perch;Wait=7+(float)random.NextDouble()*8; }
    else if(Stage==PetStage.Walk||Stage==PetStage.ClimbDown) { Stage=PetStage.Idle;Wait=3+(float)random.NextDouble()*5; }
   }
   ClampPosition();
  }
 }

 static class Native {
  [StructLayout(LayoutKind.Sequential)] public struct ICONINFO { [MarshalAs(UnmanagedType.Bool)]public bool icon;public uint hotX,hotY;public IntPtr mask,color; }
  [StructLayout(LayoutKind.Sequential)] public struct POINT { public int x,y;public POINT(int a,int b){x=a;y=b;} }
  [StructLayout(LayoutKind.Sequential)] public struct SIZE { public int cx,cy;public SIZE(int x,int y){cx=x;cy=y;} }
  [StructLayout(LayoutKind.Sequential,Pack=1)] public struct BLEND { public byte operation,flags,alpha,format; }
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int left,top,right,bottom;public Rectangle Value { get { return Rectangle.FromLTRB(left,top,right,bottom); } } }
  public delegate bool EnumProc(IntPtr window,IntPtr parameter);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc callback,IntPtr parameter);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr window);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr window);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr window,out RECT rect);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr window,out uint process);
  [DllImport("user32.dll",EntryPoint="GetWindowLongW")] public static extern int GetWindowLong(IntPtr window,int index);
  [DllImport("user32.dll",EntryPoint="SetWindowLongW")] public static extern int SetWindowLong(IntPtr window,int index,int value);
  [DllImport("user32.dll")] public static extern IntPtr GetWindow(IntPtr window,uint command);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern IntPtr WindowFromPoint(POINT point);
  [DllImport("user32.dll")] public static extern IntPtr CreateIconIndirect(ref ICONINFO info);
  [DllImport("user32.dll")] public static extern bool DestroyCursor(IntPtr cursor);
  [DllImport("user32.dll")] public static extern IntPtr SetCursor(IntPtr cursor);
  [DllImport("user32.dll")] public static extern IntPtr GetCursor();
  [DllImport("dwmapi.dll")] public static extern int DwmGetWindowAttribute(IntPtr window,int attribute,out int value,int size);
  [DllImport("user32.dll")] public static extern IntPtr GetDC(IntPtr window);
  [DllImport("user32.dll")] public static extern int ReleaseDC(IntPtr window,IntPtr dc);
  [DllImport("gdi32.dll")] public static extern IntPtr CreateCompatibleDC(IntPtr dc);
  [DllImport("gdi32.dll")] public static extern bool DeleteDC(IntPtr dc);
  [DllImport("gdi32.dll")] public static extern IntPtr SelectObject(IntPtr dc,IntPtr value);
  [DllImport("gdi32.dll")] public static extern bool DeleteObject(IntPtr value);
  [DllImport("user32.dll",SetLastError=true)] public static extern bool UpdateLayeredWindow(IntPtr window,IntPtr screen,ref POINT destination,ref SIZE size,IntPtr source,ref POINT zero,int color,ref BLEND blend,int flags);
  [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr window,IntPtr after,int x,int y,int width,int height,uint flags);
  public static void Present(IntPtr window,Bitmap image,Point position,byte opacity) {
   IntPtr screen=GetDC(IntPtr.Zero),memory=IntPtr.Zero,bitmap=IntPtr.Zero,old=IntPtr.Zero;
   try {
    memory=CreateCompatibleDC(screen);bitmap=image.GetHbitmap(Color.FromArgb(0));
    if(memory==IntPtr.Zero||bitmap==IntPtr.Zero)throw new System.ComponentModel.Win32Exception();
    old=SelectObject(memory,bitmap);POINT destination=new POINT(position.X,position.Y),zero=new POINT(0,0);
    SIZE size=new SIZE(image.Width,image.Height);BLEND blend=new BLEND();blend.alpha=opacity;blend.format=1;
    if(!UpdateLayeredWindow(window,screen,ref destination,ref size,memory,ref zero,0,ref blend,2))throw new System.ComponentModel.Win32Exception();
   } finally {if(old!=IntPtr.Zero)SelectObject(memory,old);if(bitmap!=IntPtr.Zero)DeleteObject(bitmap);if(memory!=IntPtr.Zero)DeleteDC(memory);if(screen!=IntPtr.Zero)ReleaseDC(IntPtr.Zero,screen);}
  }
 }

 // Visual decorations are a separate permanently click-through surface. They
 // cannot enlarge the interactive kitten mask or steal mouse input/focus.
 sealed class AmbienceWindow : Form {
  double nextDraw;string lastKey;int lastWidth;
  public AmbienceWindow() {
   FormBorderStyle=FormBorderStyle.None;ShowInTaskbar=false;TopMost=true;StartPosition=FormStartPosition.Manual;
   Size=new Size(Painter.Width,Painter.Height);Text="Атмосфера · Мурчик";
  }
  protected override bool ShowWithoutActivation {get{return true;}}
  protected override CreateParams CreateParams {get{CreateParams value=base.CreateParams;value.ExStyle|=0x80000|0x20|0x80|0x08000000;return value;}}
  protected override void WndProc(ref Message message) {
   if(message.Msg==0x21){message.Result=new IntPtr(3);return;}
   if(message.Msg==0x84){message.Result=new IntPtr(-1);return;}
   base.WndProc(ref message);
  }
  public void Present(AmbienceSnapshot sky,double animation,Point position,IntPtr kitten,int spriteWidth) {
   if(IsDisposed)return;
   bool redraw=!Visible||animation>=nextDraw||sky.MinuteKey!=lastKey||lastWidth!=spriteWidth;
   if(!Visible)Show();
   if(redraw) {
    Size=Painter.SurfaceSize(spriteWidth);
    using(Bitmap image=Painter.BuildAmbience(sky,animation,spriteWidth))Native.Present(Handle,image,position,255);
    nextDraw=animation+.10;lastKey=sky.MinuteKey;lastWidth=spriteWidth;
    Native.SetWindowPos(Handle,kitten,0,0,0,0,0x13);
   } else Native.SetWindowPos(Handle,kitten,position.X,position.Y,0,0,0x11);
  }
 }

 sealed class Sprites : IDisposable {
  readonly Bitmap[] sourceFrames=new Bitmap[16];
  readonly Dictionary<int,Bitmap[]> cache=new Dictionary<int,Bitmap[]>();
  public int CachedSizes {get{return cache.Count;}}
  public Sprites(string path) {
   using(Bitmap original=new Bitmap(path))Load(original);
  }
  public Sprites(Bitmap original) {Load(original);}
  void Load(Bitmap original) {
   try {
    if(original.Width!=original.Height||original.Width<512||original.Width>4096)throw new InvalidDataException();
    using(Bitmap normalized=new Bitmap(original.Width,original.Height,PixelFormat.Format32bppArgb)) {
     using(Graphics g=Graphics.FromImage(normalized)){g.CompositingMode=CompositingMode.SourceCopy;g.DrawImageUnscaled(original,0,0);}
     BitmapData data=normalized.LockBits(new Rectangle(0,0,normalized.Width,normalized.Height),ImageLockMode.ReadWrite,PixelFormat.Format32bppArgb);
     try {
      byte[] row=new byte[Math.Abs(data.Stride)];
      for(int y=0;y<normalized.Height;y++) {
       IntPtr location=IntPtr.Add(data.Scan0,y*data.Stride);Marshal.Copy(location,row,0,row.Length);
       for(int x=0;x<normalized.Width;x++)if(row[x*4+3]<12){row[x*4]=row[x*4+1]=row[x*4+2]=row[x*4+3]=0;}
       Marshal.Copy(row,0,location,row.Length);
      }
     } finally {normalized.UnlockBits(data);}
     for(int i=0;i<16;i++) {
      int col=i%4,row=i/4;int left=col*normalized.Width/4,top=row*normalized.Height/4;
      Rectangle cell=new Rectangle(left,top,(col+1)*normalized.Width/4-left,(row+1)*normalized.Height/4-top);
      // Preserve the source detail. Each display size is sampled directly from
      // its isolated native cell once, never enlarged from the old 140px frame.
      using(Bitmap crop=normalized.Clone(cell,PixelFormat.Format32bppPArgb))sourceFrames[i]=Sample(crop,Math.Min(512,Math.Max(cell.Width,cell.Height)));
     }
    }
   }catch{Dispose();throw;}
  }
  static Bitmap Sample(Bitmap source,int width) {
   Bitmap target=new Bitmap(width,width,PixelFormat.Format32bppPArgb);
   try {
    using(Graphics g=Graphics.FromImage(target))using(ImageAttributes wrap=new ImageAttributes()) {
     g.CompositingMode=CompositingMode.SourceCopy;g.CompositingQuality=CompositingQuality.HighQuality;
     g.InterpolationMode=InterpolationMode.HighQualityBicubic;g.PixelOffsetMode=PixelOffsetMode.HighQuality;
     wrap.SetWrapMode(WrapMode.TileFlipXY);
     g.DrawImage(source,new Rectangle(0,0,width,width),0,0,source.Width,source.Height,GraphicsUnit.Pixel,wrap);
    }
    return target;
   }catch{target.Dispose();throw;}
  }
  public Bitmap NativeFrame(int index) {return sourceFrames[Math.Max(0,Math.Min(15,index))];}
  public Bitmap Frame(int index) {return Frame(index,Painter.Width);}
  public Bitmap Frame(int index,int width) {
   if(!Painter.IsSpriteSize(width))throw new ArgumentOutOfRangeException("width");
   Bitmap[] frames;
   if(!cache.TryGetValue(width,out frames)) {
    frames=new Bitmap[16];
    try{for(int i=0;i<16;i++)frames[i]=Sample(sourceFrames[i],width);}
    catch{foreach(Bitmap frame in frames)if(frame!=null)frame.Dispose();throw;}
    cache.Add(width,frames);
   }
   return frames[Math.Max(0,Math.Min(15,index))];
  }
  public void Dispose(){foreach(Bitmap[] frames in cache.Values)foreach(Bitmap frame in frames)if(frame!=null)frame.Dispose();cache.Clear();for(int i=0;i<sourceFrames.Length;i++)if(sourceFrames[i]!=null){sourceFrames[i].Dispose();sourceFrames[i]=null;}}
 }

 static class Animation {
  public static int Frame(double time,bool moving,bool climbing,bool sleeping,bool stretch) {
   if(moving){double rate=climbing?5:7;return (climbing?12:0)+(int)((time%(4/rate))*rate);}
   if(stretch)return 11;
   if(sleeping)return 9+(int)((time%2.2)/1.1);
   double cycle=time%5;
   if(cycle>=4.65&&cycle<4.75)return 5;
   if(cycle>=4.75&&cycle<4.85)return 6;
   if(cycle>=4.85&&cycle<4.95)return 5;
   if((int)((time%15)/5)==2&&cycle>=2.8&&cycle<3.6)return 7;
   return 4;
  }
 }

 enum PetAction { None,Chat,Menu }
 static class Painter {
  // All artwork, chrome and interaction geometry use this single logical base.
  // Only the final surface is scaled; native sprite cells retain their detail.
  public const int Width=140,Height=163,DefaultSpriteWidth=200;
  public static bool IsSpriteSize(int width){return width==140||width==200||width==260;}
  public static float Scale(int width){if(!IsSpriteSize(width))throw new ArgumentOutOfRangeException("width");return width/(float)Width;}
  public static int Pixel(float logical,int width){return (int)Math.Round(logical*Scale(width));}
  public static Size SurfaceSize(int width){return new Size(width,(int)Math.Ceiling(Height*Scale(width)));}
  public static PointF ToLogical(Point physical,int width){float scale=Scale(width);return new PointF(physical.X/scale,physical.Y/scale);}
  public static PetAction ActionAt(Point physical,int width) {
   PointF logical=ToLogical(physical,width);
   if(logical.X<0||logical.X>=Width||logical.Y<138||logical.Y>=Height)return PetAction.None;
   return logical.X>=113?PetAction.Menu:PetAction.Chat;
  }
  static Matrix BodyMatrix(bool facingLeft,bool petting,double animation) {
   Matrix value=new Matrix();
   if(petting){value.Translate(70,112);value.Rotate((float)(Math.Sin(animation*5)*1.8));value.Scale(1,(float)(1+Math.Sin(animation*8)*.012));value.Translate(-70,-112);}
   if(facingLeft){value.Translate(Width,0);value.Scale(-1,1);}
   return value;
  }
  public static bool BodyContains(Bitmap sprite,Point point,bool facingLeft,bool moving,bool petting,double animation) {
   return BodyContains(sprite,point,facingLeft,moving,petting,animation,Width);
  }
  public static bool BodyContains(Bitmap sprite,Point point,bool facingLeft,bool moving,bool petting,double animation,int spriteWidth) {
   float scale=Scale(spriteWidth);PointF logical=ToLogical(point,spriteWidth);
   if(logical.X<0||logical.X>=Width||logical.Y<0||logical.Y>=138)return false;
   PointF[] mapped={new PointF((point.X+.5f)/scale,(point.Y+.5f)/scale)};
   using(Matrix inverse=BodyMatrix(facingLeft,petting,animation)){inverse.Invert();inverse.TransformPoints(mapped);}
   float yLogical=mapped[0].Y-(!moving?(int)(Math.Sin(animation*2)*1.2):0);
   int x=(int)Math.Floor(mapped[0].X*sprite.Width/Width),y=(int)Math.Floor(yLogical*sprite.Height/Width);
   return x>=0&&y>=0&&x<sprite.Width&&y<sprite.Height&&sprite.GetPixel(x,y).A>=64;
  }
  public static Bitmap Build(Sprites sprites,int frame,bool facingLeft,bool moving,bool fullscreen,double animation,bool heart,bool petting=false,int spriteWidth=DefaultSpriteWidth) {
   float scale=Scale(spriteWidth);Size size=SurfaceSize(spriteWidth);
   Bitmap image=new Bitmap(size.Width,size.Height,PixelFormat.Format32bppPArgb);
   using(Graphics g=Graphics.FromImage(image)) {
    g.Clear(Color.Transparent);g.SmoothingMode=SmoothingMode.AntiAlias;g.InterpolationMode=InterpolationMode.HighQualityBicubic;g.PixelOffsetMode=PixelOffsetMode.HighQuality;
    using(Matrix body=BodyMatrix(facingLeft,petting,animation)) {
     body.Scale(scale,scale,MatrixOrder.Append);g.Transform=body;
     g.DrawImage(sprites.Frame(frame,spriteWidth),new Rectangle(0,(!moving?(int)(Math.Sin(animation*2)*1.2):0),Width,Width));
    }
    g.ResetTransform();g.ScaleTransform(scale,scale);
    Rectangle pill=new Rectangle(5,140,106,21);using(GraphicsPath shape=Round(pill,10))using(Brush fill=new SolidBrush(Color.FromArgb(fullscreen?140:235,255,245,237)))g.FillPath(fill,shape);
    using(GraphicsPath shape=Round(new Rectangle(115,140,21,21),8))using(Brush fill=new SolidBrush(Color.FromArgb(235,255,245,237)))g.FillPath(fill,shape);
    using(Pen pen=new Pen(Color.FromArgb(105,66,62),1.5f))for(int line=0;line<3;line++)g.DrawLine(pen,121,146+line*4,130,146+line*4);
    using(Font font=new Font("Segoe UI",8,FontStyle.Bold))using(Brush text=new SolidBrush(Color.FromArgb(105,66,62)))using(StringFormat format=new StringFormat()) {
     format.Alignment=StringAlignment.Center;format.LineAlignment=StringAlignment.Center;g.DrawString("Написать папе",font,text,pill,format);
    }
    if(petting) {
     using(Font font=new Font("Segoe UI Symbol",12,FontStyle.Bold))for(int i=0;i<3;i++) {
      double progress=(animation*.65+i/3.0)%1;
      using(Brush brush=new SolidBrush(Color.FromArgb((int)(180*(1-progress)),225,133,148)))g.DrawString("♥",font,brush,(i%2==0?8:112)+(float)Math.Sin(animation*2+i)*3,45-(float)progress*32);
     }
    } else if(heart)using(Font font=new Font("Segoe UI Symbol",16,FontStyle.Bold))using(Brush brush=new SolidBrush(Color.FromArgb(225,133,148)))g.DrawString("♥",font,brush,109,8);
   }
   return image;
  }
  public static Bitmap BuildAmbience(AmbienceSnapshot snapshot,double animation,int spriteWidth) {
   Size size=SurfaceSize(spriteWidth);Bitmap image=new Bitmap(size.Width,size.Height,PixelFormat.Format32bppPArgb);
   using(Graphics g=Graphics.FromImage(image)){g.Clear(Color.Transparent);g.ScaleTransform(Scale(spriteWidth),Scale(spriteWidth));PetAmbience.Draw(g,snapshot,animation);}
   return image;
  }
  static GraphicsPath Round(Rectangle rect,int radius) {
   GraphicsPath path=new GraphicsPath();int d=radius*2;path.AddArc(rect.Left,rect.Top,d,d,180,90);path.AddArc(rect.Right-d,rect.Top,d,d,270,90);path.AddArc(rect.Right-d,rect.Bottom-d,d,d,0,90);path.AddArc(rect.Left,rect.Bottom-d,d,d,90,90);path.CloseFigure();return path;
  }
  public static int Preview(string root) {
   string asset=Files.SpriteAsset(root);
   string destination=Path.Combine(root,"kitten-preview.png");
   using(Sprites sprites=new Sprites(asset)) {
    for(int i=0;i<16;i++) {
     int clear=0,solid=0;Bitmap frame=sprites.Frame(i);
     for(int y=0;y<frame.Height;y++)for(int x=0;x<frame.Width;x++){byte alpha=frame.GetPixel(x,y).A;if(alpha==0)clear++;if(alpha>=200)solid++;}
     if(clear<100||solid<100)throw new InvalidDataException("sprite_alpha_invalid");
    }
    using(Bitmap preview=new Bitmap(1360,1160,PixelFormat.Format32bppArgb))using(Graphics g=Graphics.FromImage(preview)) {
     g.Clear(Color.FromArgb(246,243,238));g.SmoothingMode=SmoothingMode.AntiAlias;g.InterpolationMode=InterpolationMode.HighQualityBicubic;g.PixelOffsetMode=PixelOffsetMode.HighQuality;
     int[] frames={0,4,9,12,6};string[] labels={"Прогулка","Спокойный котёнок","Сон","Подъём","Поглаживание"};
     using(Font title=new Font("Segoe UI",15,FontStyle.Bold))using(Font label=new Font("Segoe UI",11))using(Brush ink=new SolidBrush(Color.FromArgb(85,69,64))) {
      g.DrawString("Мурчик · детали, размеры и собственный рендер",title,ink,20,15);
      string portraitPath=Path.Combine(root,"assets","kitten-portrait-v2.png");
      if(File.Exists(portraitPath))using(Bitmap portrait=new Bitmap(Files.Safe(portraitPath)))g.DrawImage(portrait,PortraitWindow.Fit(portrait.Size,new Rectangle(20,60,400,400)));
      else g.DrawImage(sprites.NativeFrame(4),new Rectangle(20,60,400,400));
      g.DrawString("Портрет: «Рассмотреть Мурчика»",label,ink,25,470);
      g.DrawString("Размеры на рабочем столе · без увеличения снимка экрана",label,ink,460,68);
      int[] sizes={140,200,260};int[] positions={470,680,960};string[] sizeLabels={"Маленький · 140","Обычный · 200","Крупный · 260"};
      for(int i=0;i<sizes.Length;i++) {
       using(Bitmap frame=Build(sprites,4,false,false,false,.4,false,false,sizes[i]))g.DrawImageUnscaled(frame,positions[i],450-frame.Height);
       g.DrawString(sizeLabels[i],label,ink,positions[i],470);
      }
      g.DrawString("Анимационные позы · размер 200",title,ink,20,515);
      for(int i=0;i<frames.Length;i++) {
       using(Bitmap frame=Build(sprites,frames[i],false,i==0||i==3,false,.4,i==4,i==4))g.DrawImageUnscaled(frame,25+i*265,555);
       if(i==4)using(Bitmap palm=PettingCursors.Image(2))g.DrawImageUnscaled(palm,25+i*265+84,570);
       g.DrawString(labels[i],label,ink,25+i*265,795);
      }
      AmbienceSnapshot[] skies={
       new AmbienceSnapshot(PetDayPart.Morning,PetSeason.Spring,PetWeather.Fair,.55f,true,"preview"),
       new AmbienceSnapshot(PetDayPart.Day,PetSeason.Summer,PetWeather.Fair,1,true,"preview"),
       new AmbienceSnapshot(PetDayPart.Evening,PetSeason.Autumn,PetWeather.Rain,.16f,true,"preview"),
       new AmbienceSnapshot(PetDayPart.Night,PetSeason.Winter,PetWeather.Snow,0,true,"preview")};
      g.DrawString("Маленький мир · единый масштаб, сказочная погода",title,ink,20,835);
      for(int i=0;i<skies.Length;i++) {
       using(Bitmap sky=BuildAmbience(skies[i],.8,DefaultSpriteWidth))g.DrawImageUnscaled(sky,40+i*325,875);
       using(Bitmap frame=Build(sprites,i==3?9:4,false,false,false,.8,false))g.DrawImageUnscaled(frame,40+i*325,875);
       g.DrawString(skies[i].PartLabel+" · "+skies[i].SeasonLabel,label,ink,40+i*325,1120);
      }
     }
     using(FileStream output=new FileStream(destination,FileMode.CreateNew,FileAccess.Write,FileShare.None))preview.Save(output,ImageFormat.Png);
    }
   }
   Console.WriteLine("{\"status\":\"render_preview_ok\",\"file\":\"kitten-preview.png\",\"asset_frames_tested\":16,\"desktop_sizes\":[140,200,260],\"ambience_panels\":4,\"desktop_pixels_read\":false,\"network\":false}");return 0;
  }
 }

 sealed class PetWindow : Form {
  int spriteWidth=Painter.DefaultSpriteWidth;
  int PetWidth {get{return Painter.SurfaceSize(spriteWidth).Width;}} int PetHeight {get{return Painter.SurfaceSize(spriteWidth).Height;}}
  readonly bool diagnostic;
  readonly Sprites sprites;readonly Motion motion;readonly EventWaitHandle showEvent;
  readonly Petting petting=new Petting();readonly PettingCursors palms=new PettingCursors();readonly PurrPlayer purr=new PurrPlayer();
  readonly System.Windows.Forms.Timer timer=new System.Windows.Forms.Timer();
  readonly Stopwatch clock=Stopwatch.StartNew();
  readonly NotifyIcon tray=new NotifyIcon();readonly ContextMenuStrip menu=new ContextMenuStrip();
  readonly AmbienceWindow ambientWindow=new AmbienceWindow();
  readonly TricksHost tricks=new TricksHost();TrickPose trickPose=new TrickPose();bool greeted;
  AmbienceSnapshot atmosphere=PetAmbience.Unknown();double nextAmbience;
  readonly Dictionary<PetMode,ToolStripMenuItem> modeItems=new Dictionary<PetMode,ToolStripMenuItem>();
  readonly Dictionary<int,ToolStripMenuItem> sizeItems=new Dictionary<int,ToolStripMenuItem>();
  readonly ToolStripMenuItem soundItem=new ToolStripMenuItem("Мурлыканье");
  readonly ToolStripMenuItem atmosphereItem=new ToolStripMenuItem("Атмосфера");
  readonly ToolStripMenuItem atmosphereInfo=new ToolStripMenuItem();
  IList<Surface> surfaces=new List<Surface>();
  Bitmap rendered;double nextScan,animation,heartUntil,stretchUntil;bool dragging,moved;Point dragOffset,startCursor;
  int bodyFrame=4,bodyWidth=Painter.DefaultSpriteWidth;bool bodyFacingLeft,bodyMoving,bodyStroking;double bodyTime;
  PortraitWindow portrait;
  PetStage lastStage=PetStage.Idle;bool lastFullscreen;PetMode lastMode=PetMode.Roam;
  public PetWindow(string asset,EventWaitHandle signal,bool diagnosticMode) {
   diagnostic=diagnosticMode;sprites=new Sprites(asset);showEvent=signal;
   motion=new Motion(Screen.PrimaryScreen.WorkingArea,PetWidth,PetHeight,Environment.TickCount);
   atmosphere=PetAmbience.ReadClock(delegate{return DateTime.Now;});motion.NightRest=atmosphere.NightRest;
   FormBorderStyle=FormBorderStyle.None;ShowInTaskbar=diagnostic;TopMost=true;StartPosition=FormStartPosition.Manual;
   Size=new Size(PetWidth,PetHeight);Location=new Point((int)motion.X,(int)motion.Y);Text=diagnostic?"Котёнок · Проверка":"Котёнок · Пространство";
   menu.Items.Add("Написать папе",null,delegate{StartChat();});
   menu.Items.Add("Научить трюку",null,delegate{StartTricks();});menu.Items.Add("Остановить трюк",null,delegate{StopTrick();});menu.Items.Add(new ToolStripSeparator());
   AddMode("Гулять по экрану",PetMode.Roam);AddMode("Спокойный котёнок",PetMode.Calm);AddMode("Тихий уголок",PetMode.Quiet);
   ToolStripMenuItem sizes=new ToolStripMenuItem("Размер котёнка");menu.Items.Add(sizes);
   AddSize(sizes,"Маленький · 140",140);AddSize(sizes,"Обычный · 200",200);AddSize(sizes,"Крупный · 260",260);
   menu.Items.Add("Рассмотреть Мурчика",null,delegate{ShowPortrait();});
   soundItem.CheckOnClick=true;soundItem.Checked=true;soundItem.CheckedChanged+=delegate{purr.Enabled=soundItem.Checked;if(!soundItem.Checked)purr.Stop();};menu.Items.Add(soundItem);
   atmosphereItem.CheckOnClick=true;atmosphereItem.Checked=true;atmosphereItem.CheckedChanged+=delegate{if(!atmosphereItem.Checked)ambientWindow.Hide();};menu.Items.Add(atmosphereItem);
   atmosphereInfo.Enabled=false;atmosphereInfo.Text=atmosphere.Description;menu.Items.Add(atmosphereInfo);
   menu.Opening+=delegate{StopTrick();CancelPetting();};
   menu.Items.Add(new ToolStripSeparator());menu.Items.Add("Закрыть котёнка",null,delegate{Close();});
   tray.Icon=SystemIcons.Application;tray.Text="Котёнок — Пространство";tray.ContextMenuStrip=menu;tray.Visible=true;
   UpdateModeMenu();
   tray.DoubleClick+=delegate{StartChat();};
   // A 33 ms request can round up to about 47 ms on a coarse Windows timer.
   // 25 ms gives roughly 31 ms there. Motion uses measured elapsed time.
   timer.Interval=25;timer.Tick+=Tick;Shown+=delegate{Render();timer.Start();};
  }
  protected override bool ShowWithoutActivation { get {return true;} }
  protected override void OnVisibleChanged(EventArgs e){base.OnVisibleChanged(e);if(!Visible&&ambientWindow!=null&&!ambientWindow.IsDisposed)ambientWindow.Hide();}
  protected override CreateParams CreateParams { get {CreateParams value=base.CreateParams;value.ExStyle|=0x80000|0x08000000;if(diagnostic){value.ExStyle&=~0x80;value.ExStyle|=0x40000;}else value.ExStyle|=0x80;return value;} }
  void AddMode(string title,PetMode value) {
   ToolStripMenuItem item=new ToolStripMenuItem(title);item.Click+=delegate{StopTrick();CancelPetting();motion.SetMode(value);UpdateModeMenu();};menu.Items.Add(item);modeItems[value]=item;
  }
  void AddSize(ToolStripMenuItem parent,string title,int width) {
   ToolStripMenuItem item=new ToolStripMenuItem(title);item.Checked=width==spriteWidth;
   item.Click+=delegate {
    if(spriteWidth==width)return;StopTrick();CancelPetting();dragging=false;Capture=false;spriteWidth=width;
    Size=Painter.SurfaceSize(width);motion.SetSize(PetWidth,PetHeight);
    foreach(KeyValuePair<int,ToolStripMenuItem> pair in sizeItems)pair.Value.Checked=pair.Key==width;
    Render();
   };
   parent.DropDownItems.Add(item);sizeItems.Add(width,item);
  }
  void ShowPortrait() {
   try {
    if(portrait==null||portrait.IsDisposed)portrait=new PortraitWindow(Files.Safe(Path.Combine(AppDomain.CurrentDomain.BaseDirectory,"assets","kitten-portrait-v2.png")));
    portrait.Show();portrait.Activate();
   }catch{tray.ShowBalloonTip(4000,"Мурчик","Портрет пока недоступен. Сам котёнок продолжает работать.",ToolTipIcon.Info);}
  }
  void UpdateModeMenu() {
   if(IsHandleCreated){int style=Native.GetWindowLong(Handle,-20);Native.SetWindowLong(Handle,-20,Motion.MouseStyle(style,motion.Fullscreen));}
   foreach(KeyValuePair<PetMode,ToolStripMenuItem> item in modeItems)item.Value.Checked=item.Key==motion.Mode;
   string label=motion.Fullscreen?"тихо во время полного экрана":motion.Mode==PetMode.Roam?"гуляет":motion.Mode==PetMode.Calm?"спокойный":"тихий уголок";
   tray.Text="Котёнок — "+label;lastMode=motion.Mode;lastFullscreen=motion.Fullscreen;
  }
  protected override void WndProc(ref Message message) {
   if(message.Msg==0x20&&((long)message.LParam&0xffff)==1&&!motion.Fullscreen) {
    Point point=PointToClient(Cursor.Position);
    if(!dragging&&!menu.Visible&&BodyAt(point)&&motion.Mode!=PetMode.Quiet){Native.SetCursor(palms.Frame(clock.Elapsed.TotalSeconds,petting.Active(clock.Elapsed.TotalSeconds)));message.Result=new IntPtr(1);return;}
    if(Painter.ActionAt(point,spriteWidth)!=PetAction.None){Native.SetCursor(Cursors.Hand.Handle);message.Result=new IntPtr(1);return;}
   }
   if(message.Msg==0x21){message.Result=new IntPtr(3);return;} // MA_NOACTIVATE.
   if(message.Msg==0x84&&rendered!=null) {
    if(motion.Fullscreen){message.Result=new IntPtr(-1);return;}
    long raw=message.LParam.ToInt64();Point point=PointToClient(new Point((short)(raw&0xffff),(short)((raw>>16)&0xffff)));
    bool hit=point.X>=0&&point.Y>=0&&point.X<rendered.Width&&point.Y<rendered.Height&&rendered.GetPixel(point.X,point.Y).A>=64;
    message.Result=new IntPtr(hit?1:-1);return;
   }
   base.WndProc(ref message);
  }
  protected override void OnMouseDown(MouseEventArgs e) {
   base.OnMouseDown(e);
   StopTrick();CancelPetting();
   if(e.Button==MouseButtons.Right){menu.Show(this,e.Location);return;}
   if(e.Button==MouseButtons.Left){dragging=true;moved=false;dragOffset=e.Location;startCursor=Cursor.Position;Capture=true;}
  }
  protected override void OnMouseMove(MouseEventArgs e) {
   base.OnMouseMove(e);
   if(!dragging){SamplePetting(e.Location);return;}
   Point cursor=Cursor.Position;
   if(Math.Abs(cursor.X-startCursor.X)+Math.Abs(cursor.Y-startCursor.Y)>5)moved=true;
   if(moved){motion.SetArea(Screen.FromPoint(cursor).WorkingArea);motion.Place(cursor.X-dragOffset.X,cursor.Y-dragOffset.Y);Render();}
  }
  bool BodyAt(Point point) {
   return rendered!=null&&Painter.BodyContains(sprites.Frame(bodyFrame,bodyWidth),point,bodyFacingLeft,bodyMoving,bodyStroking,bodyTime,bodyWidth);
  }
  void SamplePetting(Point client) {
   bool blocked=dragging||motion.Fullscreen||motion.Mode==PetMode.Quiet||menu.Visible;
   Point screen=Cursor.Position;
   bool own=Native.WindowFromPoint(new Native.POINT(screen.X,screen.Y))==Handle;
   double now=clock.Elapsed.TotalSeconds;bool wasStroking=petting.Active(now);
   if(petting.Sample(screen,own&&BodyAt(client),blocked,now)) {
    heartUntil=Math.Max(heartUntil,now+.8);if(!wasStroking)tricks.Trigger("petted",now,blocked);
   }
   if(!petting.Hover&&!trickPose.Purr)purr.Stop();
  }
  void CancelPetting(){petting.Cancel();if(!trickPose.Purr)purr.Stop();}
  void StopTrick(){tricks.Stop(TrickFiles.UtcNow());trickPose=new TrickPose();purr.Stop();}
  protected override void OnMouseEnter(EventArgs e){base.OnMouseEnter(e);SamplePetting(PointToClient(Cursor.Position));}
  protected override void OnMouseLeave(EventArgs e){base.OnMouseLeave(e);CancelPetting();}
  protected override void OnMouseCaptureChanged(EventArgs e) {
   base.OnMouseCaptureChanged(e);CancelPetting();
   if(!Capture&&dragging){dragging=false;moved=false;}
  }
  protected override void OnMouseUp(MouseEventArgs e) {
   base.OnMouseUp(e);if(e.Button!=MouseButtons.Left)return;bool click=dragging&&!moved;dragging=false;Capture=false;
   if(click){heartUntil=animation+2;PetAction action=Painter.ActionAt(e.Location,spriteWidth);if(action==PetAction.Menu)menu.Show(this,new Point(0,Painter.Pixel(138,spriteWidth)));else if(action==PetAction.Chat)StartChat();}
  }
  protected override void OnMouseDoubleClick(MouseEventArgs e){base.OnMouseDoubleClick(e);if(e.Button==MouseButtons.Left&&Painter.ToLogical(e.Location,spriteWidth).Y<138)StartChat();}
  void Tick(object sender,EventArgs args) {
   double now=clock.Elapsed.TotalSeconds;double dt=now-animation;animation=now;
   if(showEvent.WaitOne(0)){Show();motion.SetMode(PetMode.Calm);heartUntil=now+2;UpdateModeMenu();}
   bool fullscreen=ForegroundFullscreen();
   bool trickBlocked=fullscreen||motion.Mode==PetMode.Quiet||dragging||menu.Visible;
   trickPose=tricks.Advance(now,TrickFiles.UtcNow(),trickBlocked);
   if(!greeted){greeted=true;tricks.Trigger("greet",now,trickBlocked);trickPose=tricks.Advance(now,TrickFiles.UtcNow(),trickBlocked);}
   if(now>=nextAmbience){atmosphere=PetAmbience.ReadClock(delegate{return DateTime.Now;});motion.NightRest=atmosphere.NightRest;atmosphereInfo.Text=atmosphere.Description;nextAmbience=now+30;}
   Point screen=Cursor.Position;
   bool ownBody=petting.Hover&&!dragging&&!menu.Visible&&!fullscreen&&motion.Mode!=PetMode.Quiet&&Native.WindowFromPoint(new Native.POINT(screen.X,screen.Y))==Handle&&BodyAt(PointToClient(screen));
   if(!ownBody)CancelPetting();
   if(now>=nextScan) {
    bool firstScan=nextScan==0;
    surfaces=ReadSurfaces(motion.Area);
    // Keep the first scan immediate, then offset it from the tricks file poll.
    nextScan=now+(firstScan?1:2);
   }
   if(!dragging&&(!trickPose.Active||fullscreen||motion.Mode==PetMode.Quiet)&&(!petting.Hover||fullscreen||motion.Mode==PetMode.Quiet))motion.Step((float)dt,surfaces,fullscreen);
   bool stroking=ownBody&&petting.Active(now);
   purr.SetActive(stroking||trickPose.Purr);
   if(ownBody)Native.SetCursor(palms.Frame(now,stroking));
   if(motion.Mode!=lastMode||motion.Fullscreen!=lastFullscreen)UpdateModeMenu();
   if(motion.Stage!=lastStage){if(motion.Stage==PetStage.Idle)stretchUntil=animation+.65;lastStage=motion.Stage;}
   Render();
  }
  bool ForegroundFullscreen() {
   IntPtr foreground=Native.GetForegroundWindow();if(foreground==IntPtr.Zero)return false;
   uint pid;Native.GetWindowThreadProcessId(foreground,out pid);Native.RECT rect;
   if(!Native.GetWindowRect(foreground,out rect))return false;
   return Motion.IsFullscreen(Screen.FromHandle(foreground).Bounds,rect.Value,Native.IsWindowVisible(foreground)&&!Native.IsIconic(foreground),pid==(uint)Process.GetCurrentProcess().Id);
  }
  IList<Surface> ReadSurfaces(Rectangle area) {
   List<Surface> values=new List<Surface>();uint self=(uint)Process.GetCurrentProcess().Id;
   Native.EnumWindows(delegate(IntPtr window,IntPtr unused){
    try {
     if(!Native.IsWindowVisible(window)||Native.IsIconic(window)||Native.GetWindow(window,4)!=IntPtr.Zero)return true;
     uint pid;Native.GetWindowThreadProcessId(window,out pid);if(pid==self)return true;
     int style=Native.GetWindowLong(window,-16),extended=Native.GetWindowLong(window,-20);
     if((extended&0x80)!=0||(style&0x40000000)!=0||((style&0x00c00000)!=0x00c00000&&(style&0x00040000)==0))return true;
     int cloaked;if(Native.DwmGetWindowAttribute(window,14,out cloaked,4)==0&&cloaked!=0)return true;
     Native.RECT rect;if(Native.GetWindowRect(window,out rect)&&Motion.UsableSurface(rect.Value,area,PetHeight))values.Add(new Surface(window.ToInt64(),rect.Value));
    } catch { }return true;
   },IntPtr.Zero);return values;
  }
  void Render() {
   if(IsDisposed||!IsHandleCreated)return;
   if(!Visible||WindowState==FormWindowState.Minimized){if(ambientWindow.Visible)ambientWindow.Hide();return;}
   bool stroking=petting.Active(animation)&&!motion.Fullscreen&&motion.Mode!=PetMode.Quiet&&!dragging;
   bool moving=!trickPose.Active&&!petting.Hover&&Math.Abs(motion.TargetX-motion.X)+Math.Abs(motion.TargetY-motion.Y)>.5f;
   bool climbing=motion.Stage==PetStage.ClimbUp||motion.Stage==PetStage.ClimbDown;
   bool sleeping=motion.Mode==PetMode.Quiet||motion.Fullscreen||(motion.NightRest&&!moving&&!petting.Hover);
   int frame=stroking?6:Animation.Frame(animation,moving,climbing,sleeping,stretchUntil>animation||heartUntil-animation>1.3);
   if(trickPose.Active&&trickPose.Frame>=0&&!stroking)frame=trickPose.Frame;
   bool facing=motion.FacingLeft^(trickPose.Active&&trickPose.Flip);
   Bitmap next=Painter.Build(sprites,frame,facing,moving,motion.Fullscreen,animation,heartUntil>animation||trickPose.Heart,stroking,spriteWidth);
   Point position=new Point((int)Math.Round(motion.X),(int)Math.Round(motion.Y));
   try {
    if(PetAmbience.ShouldRender(atmosphereItem.Checked,motion.Fullscreen,motion.Mode==PetMode.Quiet,atmosphere.Available))ambientWindow.Present(atmosphere,animation,position,Handle,spriteWidth);
    else if(ambientWindow.Visible)ambientWindow.Hide();
    Native.Present(Handle,next,position,(byte)(motion.Fullscreen?170:255));
   } catch {next.Dispose();throw;}
   Bitmap previous=rendered;rendered=next;if(previous!=null)previous.Dispose();
   bodyFrame=frame;bodyFacingLeft=facing;bodyMoving=moving;bodyStroking=stroking;bodyTime=animation;bodyWidth=spriteWidth;
  }
  void StartChat() {
   try {
    string root=Path.GetFullPath(AppDomain.CurrentDomain.BaseDirectory);
#if PERSONAL
    string interpreter=File.ReadAllText(Files.Safe(Path.Combine(root,"python-path.txt"))).Trim();
    string name=Path.GetFileName(interpreter).ToLowerInvariant();if(!Path.IsPathRooted(interpreter)||(name!="python.exe"&&name!="pythonw.exe"))throw new InvalidDataException();
    interpreter=Files.Safe(interpreter);string script=Files.Safe(Path.Combine(root,"kitten_messenger.py"));
    Process.Start(new ProcessStartInfo(interpreter,"-B \""+script+"\""){UseShellExecute=false,CreateNoWindow=true,WorkingDirectory=root});
#else
    string core=Files.Safe(Path.Combine(root,"ProstranstvoCore.exe"));
    Process.Start(new ProcessStartInfo(core,"kitten"){UseShellExecute=false,CreateNoWindow=true,WorkingDirectory=root});
#endif
   } catch {tray.ShowBalloonTip(5000,"Котёнок","Не удалось открыть чат. Откройте Пространство → Общение.",ToolTipIcon.Info);}
  }
  void StartTricks() {
   try {
    string root=Path.GetFullPath(AppDomain.CurrentDomain.BaseDirectory);
#if PERSONAL
    string interpreter=File.ReadAllText(Files.Safe(Path.Combine(root,"python-path.txt"))).Trim();
    string name=Path.GetFileName(interpreter).ToLowerInvariant();if(!Path.IsPathRooted(interpreter)||(name!="python.exe"&&name!="pythonw.exe"))throw new InvalidDataException();
    interpreter=Files.Safe(interpreter);string script=Files.Safe(Path.Combine(root,"pet_tricks_server.py"));
    Process.Start(new ProcessStartInfo(interpreter,"-B \""+script+"\" --open"){UseShellExecute=false,CreateNoWindow=true,WorkingDirectory=root});
#else
    string core=Files.Safe(Path.Combine(root,"ProstranstvoCore.exe"));
    Process.Start(new ProcessStartInfo(core,"tricks --open"){UseShellExecute=false,CreateNoWindow=true,WorkingDirectory=root});
#endif
   }catch{tray.ShowBalloonTip(4000,"Мурчик","Окно обучения пока недоступно. Сам котёнок продолжает работать.",ToolTipIcon.Info);}
  }
  protected override void Dispose(bool disposing) {
   if(disposing){StopTrick();CancelPetting();purr.Dispose();timer.Stop();timer.Dispose();ambientWindow.Dispose();palms.Dispose();tray.Visible=false;tray.Dispose();menu.Dispose();if(portrait!=null)portrait.Dispose();sprites.Dispose();if(rendered!=null){rendered.Dispose();rendered=null;}}
   base.Dispose(disposing);
  }
 }

 sealed class PortraitWindow : Form {
  readonly Bitmap portrait;
  public PortraitWindow(string path) {
   using(Bitmap source=new Bitmap(path)) {
    if(source.Width<256||source.Height<256||source.Width>4096||source.Height>4096)throw new InvalidDataException();
    portrait=new Bitmap(source);
   }
   Text="Мурчик · рассмотреть поближе";BackColor=Color.FromArgb(255,250,241);DoubleBuffered=true;
   Rectangle work=Screen.PrimaryScreen.WorkingArea;
   StartPosition=FormStartPosition.CenterScreen;MinimumSize=new Size(Math.Min(320,work.Width),Math.Min(360,work.Height));
   ClientSize=new Size(Math.Min(600,Math.Max(240,work.Width-60)),Math.Min(640,Math.Max(280,work.Height-80)));
   ResizeRedraw=true;ShowInTaskbar=true;
  }
  public static Rectangle Fit(Size image,Rectangle box) {
   double scale=Math.Min(box.Width/(double)image.Width,box.Height/(double)image.Height);
   int width=Math.Max(1,(int)Math.Round(image.Width*scale)),height=Math.Max(1,(int)Math.Round(image.Height*scale));
   return new Rectangle(box.X+(box.Width-width)/2,box.Y+(box.Height-height)/2,width,height);
  }
  protected override void OnPaint(PaintEventArgs e) {
   base.OnPaint(e);Graphics g=e.Graphics;g.InterpolationMode=InterpolationMode.HighQualityBicubic;g.PixelOffsetMode=PixelOffsetMode.HighQuality;
   Rectangle box=new Rectangle(16,12,Math.Max(1,ClientSize.Width-32),Math.Max(1,ClientSize.Height-62));
   g.DrawImage(portrait,Fit(portrait.Size,box));
   using(Font font=new Font("Segoe UI",10))using(Brush ink=new SolidBrush(Color.FromArgb(105,81,65)))using(StringFormat format=new StringFormat()) {
    format.Alignment=StringAlignment.Center;g.DrawString("Можно изменить размер окна. Это рисунок Мурчика.",font,ink,new RectangleF(10,ClientSize.Height-38,ClientSize.Width-20,32),format);
   }
  }
  protected override void Dispose(bool disposing){if(disposing)portrait.Dispose();base.Dispose(disposing);}
 }

 static class Files {
  public static string SpriteAsset(string root) {
   string latest=Path.Combine(root,"assets","kitten-sprites-v2.png");
   if(File.Exists(latest))return Safe(latest);
   return Safe(Path.Combine(root,"assets","kitten-sprites-v1.png"));
  }
  public static string Safe(string value) {
   string full=Path.GetFullPath(value);string current=full;
   while(!String.IsNullOrEmpty(current)) {
    if(File.Exists(current)||Directory.Exists(current))if((File.GetAttributes(current)&FileAttributes.ReparsePoint)!=0)throw new IOException("Reparse point refused");
    DirectoryInfo parent=Directory.GetParent(current);current=parent==null?null:parent.FullName;
   }
   if(!File.Exists(full))throw new FileNotFoundException();return full;
  }
 }
 static class Tests {
  static void Assert(bool value){if(!value)throw new Exception("pet_self_test_failed");}
  public static int Run() {
   Rectangle area=new Rectangle(-100,20,1100,800);List<Surface> none=new List<Surface>();
   Motion m=new Motion(area,140,163,7);float initial=m.X;
   for(int i=0;i<3000;i++){m.Step(.033f,none,false);Assert(m.X>=m.Left&&m.X<=m.Right&&m.Y>=area.Top&&m.Y<=m.Floor);}
   Assert(Math.Abs(initial-m.X)>5);m.SetMode(PetMode.Calm);float still=m.X;for(int i=0;i<100;i++)m.Step(.033f,none,false);Assert(m.X==still);
   m.SetMode(PetMode.Quiet);for(int i=0;i<1500;i++)m.Step(.033f,none,false);Assert(Math.Abs(m.X-m.Right)<.1&&m.Stage==PetStage.Park);
   m.SetMode(PetMode.Roam);Surface surface=new Surface(10,new Rectangle(240,430,500,270));Assert(m.PlanPerch(surface));
   List<Surface> surfaces=new List<Surface>();surfaces.Add(surface);bool perched=false;
   for(int i=0;i<2500;i++){m.Step(.033f,surfaces,false);if(m.Stage==PetStage.Perch){perched=true;break;}}Assert(perched&&m.Y<m.Floor);
   for(int i=0;i<2000;i++)m.Step(.033f,none,true);Assert(m.PerchId==0&&Math.Abs(m.X-m.Right)<.1&&Math.Abs(m.Y-m.Floor)<.1);
   Assert(Motion.IsFullscreen(new Rectangle(0,0,1920,1080),new Rectangle(-1,-1,1922,1082),true,false));
   Assert(!Motion.IsFullscreen(new Rectangle(0,0,1920,1080),new Rectangle(0,0,1920,1032),true,false));
   Assert(!Motion.IsFullscreen(area,area,true,true)&&!Motion.IsFullscreen(area,area,false,false));
   Assert(!Motion.UsableSurface(area,area,163)&&!Motion.UsableSurface(new Rectangle(0,0,10,10),area,163));
   m.Place(300,500);float before=m.X;m.Step(Single.NaN,none,false);Assert(m.X==before);m.Step(100,none,false);Assert(Math.Abs(m.X-before)<7);
   Assert(Animation.Frame(0,false,false,false,false)==4&&Animation.Frame(3,false,false,false,false)==4);
   Assert(Animation.Frame(4.7,false,false,false,false)==5&&Animation.Frame(4.8,false,false,false,false)==6&&Animation.Frame(4.9,false,false,false,false)==5);
   Assert(Animation.Frame(2,false,false,true,false)==10&&Animation.Frame(2,false,false,false,true)==11);
   Assert(Animation.Frame(10000000,true,false,false,false)>=0&&Animation.Frame(10000000,true,true,false,false)>=12);
   Assert(Motion.MouseStyle(0x80080,true)==0x800a0);
   Assert(Motion.MouseStyle(0x800a0,false)==0x80080);
   Petting touch=new Petting();
   Assert(!touch.Sample(new Point(10,10),true,false,0)&&touch.Hover&&!touch.Active(0));
   Assert(!touch.Sample(new Point(10,10),true,false,.1)&&!touch.Sample(new Point(10,10),true,false,.2)&&!touch.Active(.2));
   Assert(!touch.Sample(new Point(17,10),true,false,.3)&&touch.Sample(new Point(25,11),true,false,.4)&&touch.Active(.4));
   Assert(touch.Active(1.1)&&!touch.Active(1.26));
   Assert(!touch.Sample(new Point(25,11),false,false,1.2)&&!touch.Hover&&!touch.Active(1.2));
   touch.Sample(new Point(0,0),true,false,2);touch.Sample(new Point(8,0),true,false,2.1);touch.Sample(new Point(16,0),true,false,2.2);
   Assert(touch.Active(2.2)&&!touch.Sample(new Point(20,0),true,true,2.3)&&!touch.Hover&&!touch.Active(2.3));
   touch.Sample(new Point(0,0),true,false,3);Assert(!touch.Sample(new Point(300,0),true,false,3.1)&&!touch.Active(3.1));
   Assert(!touch.Sample(new Point(301,0),true,false,2.9)&&!touch.Active(2.9));
   Assert(!touch.Sample(new Point(0,0),true,false,Double.NaN)&&!touch.Hover);
   Petting gentle=new Petting();gentle.Sample(new Point(40,40),true,false,0);
   bool gentleStroke=false;for(int i=1;i<=20;i++)gentleStroke|=gentle.Sample(new Point(40+i,40),true,false,i*.01);
   Assert(gentleStroke&&gentle.Active(.2));
   using(Bitmap body=new Bitmap(140,140,PixelFormat.Format32bppArgb)) {
    using(Graphics g=Graphics.FromImage(body))g.FillRectangle(Brushes.White,15,15,40,40);
    Assert(Painter.BodyContains(body,new Point(20,20),false,false,false,0)&&!Painter.BodyContains(body,new Point(20,145),false,false,false,0));
    Assert(!Painter.BodyContains(body,new Point(109,8),false,false,false,0)&&!Painter.BodyContains(body,new Point(0,0),false,false,false,0));
    Assert(Painter.BodyContains(body,new Point(119,20),true,false,false,0)&&!Painter.BodyContains(body,new Point(20,20),true,false,false,0));
   }
   int rendererChecks=0;Action<bool> renderCheck=delegate(bool value){rendererChecks++;Assert(value);};
   renderCheck(Painter.DefaultSpriteWidth==200&&!Painter.IsSpriteSize(139)&&!Painter.IsSpriteSize(400));
   using(Bitmap sheet=new Bitmap(1024,1024,PixelFormat.Format32bppArgb)) {
    using(Graphics g=Graphics.FromImage(sheet))for(int row=0;row<4;row++)for(int col=0;col<4;col++)g.FillRectangle(Brushes.White,col*256+28,row*256+28,100,100);
    using(Sprites detailed=new Sprites(sheet)) {
     renderCheck(detailed.NativeFrame(0).Width==256&&detailed.CachedSizes==0);
     foreach(int width in new[]{140,200,260}) {
      Size size=Painter.SurfaceSize(width);renderCheck(size.Width==width&&size.Height==(int)Math.Ceiling(163*width/140.0));
      Point body=new Point(Painter.Pixel(20,width),Painter.Pixel(20,width));
      renderCheck(Painter.BodyContains(detailed.Frame(0,width),body,false,false,false,0,width));
      renderCheck(!Painter.BodyContains(detailed.Frame(0,width),body,true,false,false,0,width));
      renderCheck(Painter.BodyContains(detailed.Frame(0,width),new Point(Painter.Pixel(119,width),Painter.Pixel(20,width)),true,false,false,0,width));
      renderCheck(!Painter.BodyContains(detailed.Frame(0,width),new Point(Painter.Pixel(20,width),Painter.Pixel(150,width)),false,false,false,0,width));
      renderCheck(Painter.ActionAt(new Point(Painter.Pixel(40,width),Painter.Pixel(150,width)),width)==PetAction.Chat);
      renderCheck(Painter.ActionAt(new Point(Painter.Pixel(125,width),Painter.Pixel(150,width)),width)==PetAction.Menu);
      renderCheck(Painter.ActionAt(body,width)==PetAction.None&&Painter.ActionAt(new Point(-1,150),width)==PetAction.None);
      renderCheck(Object.ReferenceEquals(detailed.Frame(0,width),detailed.Frame(0,width)));
      using(Bitmap rendered=Painter.Build(detailed,0,false,false,false,0,false,false,width)) {
       renderCheck(rendered.Size==size&&rendered.GetPixel(body.X,body.Y).A>=200);
       renderCheck(rendered.GetPixel(width-1,0).A==0);
      }
      using(Bitmap sky=Painter.BuildAmbience(PetAmbience.Unknown(),0,width))renderCheck(sky.Size==size&&sky.GetPixel(body.X,body.Y).A==0);
      Motion sized=new Motion(area,140,163,123);float bottom=sized.Y+sized.Height;sized.SetSize(width,size.Height);
      renderCheck(Math.Abs(sized.Y+sized.Height-bottom)<1&&sized.X>=sized.Left&&sized.X<=sized.Right);
      for(int i=0;i<500;i++)sized.Step(.033f,none,false);
      renderCheck(sized.X>=sized.Left&&sized.X<=sized.Right&&sized.Y>=area.Top&&sized.Y<=sized.Floor);
     }
     renderCheck(detailed.CachedSizes==3);
    }
   }
   Rectangle portraitFit=PortraitWindow.Fit(new Size(1200,600),new Rectangle(10,20,400,500));
   renderCheck(portraitFit==new Rectangle(10,170,400,200));
   using(PettingCursors hands=new PettingCursors()){for(int i=0;i<6;i++)Assert(hands.Frame(i*.12+.01,true)!=IntPtr.Zero);Assert(hands.OwnedCount==6);hands.Dispose();Assert(hands.OwnedCount==0);}
   Motion dayLife=new Motion(area,140,163,19),nightLife=new Motion(area,140,163,19);nightLife.NightRest=true;int dayWalk=0,nightWalk=0;
   for(int i=0;i<50000;i++){dayLife.Step(.033f,none,false);nightLife.Step(.033f,none,false);if(dayLife.Stage==PetStage.Walk)dayWalk++;if(nightLife.Stage==PetStage.Walk)nightWalk++;}
   Assert(nightWalk<dayWalk*.7);
   int ambienceChecks=PetAmbience.SelfTest();
   int trickChecks=PetTrickTests.Run();
   Console.WriteLine("{\"status\":\"self_test_ok\",\"checks\":"+(33+ambienceChecks+rendererChecks+trickChecks)+",\"ambience_checks\":"+ambienceChecks+",\"renderer_checks\":"+rendererChecks+",\"trick_checks\":"+trickChecks+",\"desktop_mutations\":false,\"network\":false}");return 0;
  }
 }
 static class Program {
  [STAThread] static int Main(string[] args) {
   if(args.Length==1&&args[0]=="--self-test")return Tests.Run();
   if(args.Length==1&&args[0]=="--purr-test") {
    using(PurrPlayer sound=new PurrPlayer()) {
     sound.SetActive(true);Stopwatch wait=Stopwatch.StartNew();while(!sound.IsPlaying&&wait.ElapsedMilliseconds<1800)Thread.Sleep(20);
     bool started=sound.IsPlaying;if(started)Thread.Sleep(1200);sound.Stop();
     Console.WriteLine("{\"status\":\"purr_test_finished\",\"playback_started\":"+(started?"true":"false")+",\"audible_confirmed\":false,\"network\":false}");return started?0:1;
    }
   }
   if(args.Length==1&&args[0]=="--render-preview") {
    try{return Painter.Preview(Path.GetFullPath(AppDomain.CurrentDomain.BaseDirectory));}
    catch{Console.Error.WriteLine("render_preview_failed");return 1;}
   }
   bool diagnostic=args.Length==1&&args[0]=="--diagnostic";
   if(args.Length!=0&&!diagnostic)return 2;
   string root=Path.GetFullPath(AppDomain.CurrentDomain.BaseDirectory);string key;
   using(SHA256 sha=SHA256.Create())key=BitConverter.ToString(sha.ComputeHash(Encoding.UTF8.GetBytes(root.ToUpperInvariant()))).Replace("-","");
   if(diagnostic)key+="-Diagnostic";
   using(Mutex mutex=new Mutex(false,"Local\\ProstranstvoDesktopPet-"+key)) {
    bool acquired=false;
    try {
     try {acquired=mutex.WaitOne(0,false);}catch(AbandonedMutexException){acquired=true;}
     using(EventWaitHandle signal=new EventWaitHandle(false,EventResetMode.AutoReset,"Local\\ProstranstvoDesktopPetShow-"+key)) {
      if(!acquired){signal.Set();return 0;}
      try {
       string asset=Files.SpriteAsset(root);Application.EnableVisualStyles();Application.SetCompatibleTextRenderingDefault(false);
       using(PetWindow window=new PetWindow(asset,signal,diagnostic))Application.Run(window);return 0;
      } catch {MessageBox.Show("Котёнок не смог запуститься. Откройте Пространство → Общение.","Пространство",MessageBoxButtons.OK,MessageBoxIcon.Information);return 1;}
     }
    } finally {if(acquired)mutex.ReleaseMutex();}
   }
  }
 }
}
