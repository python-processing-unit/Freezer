using System;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Text;
using System.Security.Cryptography;

internal static class Program
{
	private const string MarkerText = "ASMLSFX1";
	private static readonly byte[] MarkerBytes = Encoding.ASCII.GetBytes(MarkerText);
	// marker + payload length (int64) + SHA256 (32 bytes)
	private static readonly int FooterLength = MarkerText.Length + 8 + 32;
	private const string ManifestName = "__main_path.txt";

	public static int Main(string[] args)
	{
		string exePath = System.Reflection.Assembly.GetExecutingAssembly().Location;
		string tempRoot = Path.Combine(Path.GetTempPath(), "asmln_sfx_" + Guid.NewGuid().ToString("N"));
		Directory.CreateDirectory(tempRoot);
		string zipPath = Path.Combine(tempRoot, "payload.zip");

		try
		{
			ExtractPayload(exePath, zipPath);
			ZipFile.ExtractToDirectory(zipPath, tempRoot);
			File.Delete(zipPath);

			string manifestPath = Path.Combine(tempRoot, ManifestName);
			if (!File.Exists(manifestPath))
				throw new InvalidOperationException("Manifest not found: " + manifestPath);

			string mainRel = File.ReadAllText(manifestPath).Trim();
			if (string.IsNullOrWhiteSpace(mainRel))
				throw new InvalidOperationException("Main script path missing from manifest.");

			string asmlnPath = Path.Combine(tempRoot, "asm-lang.exe");
			if (!File.Exists(asmlnPath))
				throw new InvalidOperationException("Bundled asm-lang.exe is missing.");

			string mainPath = Path.Combine(tempRoot, mainRel);

			var psi = new ProcessStartInfo
			{
				FileName = asmlnPath,
				Arguments = Quote(mainPath),
				WorkingDirectory = tempRoot,
				UseShellExecute = false,
			};

			var proc = Process.Start(psi);
			if (proc == null)
				throw new InvalidOperationException("Failed to start asm-lang.exe");
			proc.WaitForExit();
			return proc.ExitCode;
		}
		catch (Exception ex)
		{
			Console.Error.WriteLine("ASM-Lang SFX error: " + ex);
			return 1;
		}
		finally
		{
			try { Directory.Delete(tempRoot, true); } catch { /* best-effort */ }
		}
	}

	private static string Quote(string path)
	{
		return "\"" + path + "\"";
	}

	private static void ExtractPayload(string exePath, string zipPath)
	{
		using (FileStream fs = new FileStream(exePath, FileMode.Open, FileAccess.Read, FileShare.Read))
		{
			if (fs.Length < FooterLength)
				throw new InvalidOperationException("Executable is missing payload footer.");

			fs.Seek(-MarkerBytes.Length, SeekOrigin.End);
			var markerBuffer = new byte[MarkerBytes.Length];
			int markerRead = fs.Read(markerBuffer, 0, markerBuffer.Length);
			if (markerRead != markerBuffer.Length)
				throw new InvalidOperationException("Failed to read marker.");
			if (!EqualBytes(markerBuffer, MarkerBytes))
				throw new InvalidOperationException("Marker not found; file is not a valid ASM-Lang SFX.");

			fs.Seek(-FooterLength, SeekOrigin.End);
			long payloadLen = ReadInt64(fs);

			var storedSha = new byte[32];
			int shaRead = fs.Read(storedSha, 0, storedSha.Length);
			if (shaRead != storedSha.Length)
				throw new InvalidOperationException("Failed to read stored payload SHA256.");

			if (payloadLen <= 0 || payloadLen > fs.Length)
				throw new InvalidOperationException("Invalid payload length in footer.");

			long payloadOffset = fs.Length - FooterLength - payloadLen;
			if (payloadOffset < 0)
				throw new InvalidOperationException("Calculated payload offset is invalid.");

			fs.Seek(payloadOffset, SeekOrigin.Begin);
			using (FileStream outFs = new FileStream(zipPath, FileMode.Create, FileAccess.Write, FileShare.None))
			using (var sha = SHA256.Create())
			{
				byte[] buffer = new byte[8192];
				long remaining = payloadLen;
				while (remaining > 0)
				{
					int toRead = (int)Math.Min(buffer.Length, remaining);
					int read = fs.Read(buffer, 0, toRead);
					if (read == 0)
						throw new EndOfStreamException("Unexpected end of file while copying payload.");
					sha.TransformBlock(buffer, 0, read, null, 0);
					outFs.Write(buffer, 0, read);
					remaining -= read;
				}
				sha.TransformFinalBlock(new byte[0], 0, 0);
				byte[] computed = sha.Hash;
				if (!EqualBytes(computed, storedSha))
				{
					try { outFs.Close(); File.Delete(zipPath); } catch { }
					throw new InvalidOperationException("Payload SHA256 does not match; file may be corrupt.");
				}
			}
		}
	}

	private static long ReadInt64(Stream stream)
	{
		byte[] buffer = new byte[8];
		int read = stream.Read(buffer, 0, buffer.Length);
		if (read != buffer.Length)
			throw new InvalidOperationException("Failed to read payload length.");
		return BitConverter.ToInt64(buffer, 0);
	}

	private static bool EqualBytes(byte[] a, byte[] b)
	{
		if (a.Length != b.Length) return false;
		for (int i = 0; i < a.Length; i++)
		{
			if (a[i] != b[i]) return false;
		}
		return true;
	}
}
