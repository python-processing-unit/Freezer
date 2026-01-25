using System;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.IO;
using System.Collections.Generic;

class ImgToIco
{
    static int Main(string[] args)
    {
        if (args.Length != 2) { Console.Error.WriteLine("Usage: img2ico <in> <out>"); return 2; }
        string inPath = args[0];
        string outPath = args[1];
        try
        {
            using (Image src = Image.FromFile(inPath))
            {
                int[] sizes = new int[] {256,128,64,48,32,16};
                var images = new List<byte[]>();
                var isPng = new List<bool>();
                foreach (int size in sizes)
                {
                    using (Bitmap bmp = new Bitmap(size, size))
                    {
                        using (Graphics g = Graphics.FromImage(bmp))
                        {
                            g.InterpolationMode = InterpolationMode.HighQualityBicubic;
                            g.SmoothingMode = SmoothingMode.AntiAlias;
                            g.PixelOffsetMode = PixelOffsetMode.HighQuality;
                            g.Clear(Color.Transparent);
                            float srcW = src.Width;
                            float srcH = src.Height;
                            float ratio = Math.Min(size / srcW, size / srcH);
                            int w = Math.Max(1, (int)(srcW * ratio));
                            int h = Math.Max(1, (int)(srcH * ratio));
                            int x = (size - w) / 2;
                            int y = (size - h) / 2;
                            g.DrawImage(src, x, y, w, h);
                        }

                        if (size <= 32)
                        {
                            using (var ms = new MemoryStream())
                            {
                                bmp.Save(ms, ImageFormat.Bmp);
                                byte[] bmpBytes = ms.ToArray();
                                int headerOff = 14;
                                byte[] dib = new byte[bmpBytes.Length - headerOff];
                                Array.Copy(bmpBytes, headerOff, dib, 0, dib.Length);
                                int biHeight = size * 2;
                                dib[8] = (byte)(biHeight & 0xFF);
                                dib[9] = (byte)((biHeight >> 8) & 0xFF);
                                dib[10] = (byte)((biHeight >> 16) & 0xFF);
                                dib[11] = (byte)((biHeight >> 24) & 0xFF);
                                int maskStride = ((size + 31) / 32) * 4;
                                byte[] andMask = new byte[maskStride * size];
                                byte[] final = new byte[dib.Length + andMask.Length];
                                Buffer.BlockCopy(dib, 0, final, 0, dib.Length);
                                Buffer.BlockCopy(andMask, 0, final, dib.Length, andMask.Length);
                                images.Add(final);
                                isPng.Add(false);
                            }
                        }
                        else
                        {
                            using (var ms = new MemoryStream())
                            {
                                bmp.Save(ms, ImageFormat.Png);
                                images.Add(ms.ToArray());
                                isPng.Add(true);
                            }
                        }
                    }
                }

                using (var fs = new FileStream(outPath, FileMode.Create))
                using (var bw = new BinaryWriter(fs))
                {
                    bw.Write((ushort)0);
                    bw.Write((ushort)1);
                    bw.Write((ushort)images.Count);

                    int offset = 6 + 16 * images.Count;
                    for (int i = 0; i < images.Count; i++)
                    {
                        int size = sizes[i];
                        byte width = (byte)(size >= 256 ? 0 : size);
                        byte height = (byte)(size >= 256 ? 0 : size);
                        bw.Write(width);
                        bw.Write(height);
                        bw.Write((byte)0);
                        bw.Write((byte)0);
                        bool entryIsPng = isPng[i];
                        bw.Write((ushort)(entryIsPng ? 0 : 1));
                        bw.Write((ushort)(entryIsPng ? 0 : 32));
                        bw.Write((uint)images[i].Length);
                        bw.Write((uint)offset);
                        offset += images[i].Length;
                    }

                    for (int i = 0; i < images.Count; i++)
                    {
                        bw.Write(images[i]);
                    }
                }
            }
            return 0;
        }
        catch (Exception e)
        {
            Console.Error.WriteLine(e.ToString());
            return 1;
        }
    }
}
