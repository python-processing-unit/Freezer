"""
Executable builder for ASM-Lang on Windows 10+.

Creates a self-extracting executable that bundles:
- The ASM-Lang runtime (`asmln.exe`) and its `lib/` folder.
- A main ASM-Lang script to run on startup.
- Optional additional files and folders placed at custom paths inside the bundle.

Runtime behavior of the generated EXE:
1) Extracts itself to a temp directory.
2) Runs `asmln.exe <main script>` from that temp directory.
3) Cleans up extracted files on exit.

No external dependencies are required beyond stock Windows 10 (uses the built-in
`csc.exe` compiler from the .NET Framework). A working ASM-Lang installation is
still required for the bundled runtime and libraries you provide.
"""

from __future__ import annotations

import argparse
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


MARKER = b"ASMLSFX1"
FOOTER_LEN = len(MARKER) + 8  # payload length (int64 LE) + marker
MANIFEST_NAME = "__main_path.txt"


class BuildError(Exception):
	"""Raised for predictable build-time failures."""


def parse_args(argv: list[str]) -> argparse.Namespace:
	parser = argparse.ArgumentParser(
		description="Build a self-extracting ASM-Lang executable for Windows 10+",
		formatter_class=argparse.ArgumentDefaultsHelpFormatter,
	)

	parser.add_argument(
		"asmln_exe",
		help="Path to the ASM-Lang asmln.exe to bundle",
	)
	parser.add_argument(
		"main_file",
		help="Path to the main ASM-Lang script to run on startup",
	)
	parser.add_argument(
		"--include",
		action="append",
		default=[],
		metavar="src;dest_in_exe",
		help="Include a single file; dest '.' means bundle root",
	)
	parser.add_argument(
		"--include-folder",
		action="append",
		default=[],
		metavar="src_dir;dest_in_exe",
		help=(
			"Include a folder recursively. If dest is '.', the folder is placed under the bundle root using the folder name."
		),
	)
	parser.add_argument(
		"--main-dest",
		default=".",
		help="Destination directory inside the bundle for the main script ('.' = root)",
	)
	parser.add_argument(
		"--output",
		default=None,
		help="Output path for the generated self-extracting EXE",
	)
	parser.add_argument(
		"--verbose",
		action="store_true",
		help="Enable verbose logging",
	)

	return parser.parse_args(argv)


def log(msg: str, *, verbose: bool) -> None:
	if verbose:
		print(msg)


def ensure_windows() -> None:
	if os.name != "nt":
		raise BuildError("This builder only supports Windows 10+.")


def parse_mapping(raw: str) -> tuple[Path, str]:
	if ";" not in raw:
		raise BuildError(f"Mapping must be of form 'source;dest_in_exe': {raw!r}")
	src_raw, dest_raw = raw.split(";", 1)
	src = Path(src_raw).expanduser().resolve()
	if not src.exists():
		raise BuildError(f"Included path does not exist: {src}")
	dest = dest_raw.strip() or "."
	return src, dest


def copy_main(main_path: Path, dest_rel: str, payload_root: Path) -> str:
	dest_rel = dest_rel.strip() or "."
	target_dir = payload_root if dest_rel == "." else payload_root / dest_rel
	target_dir.mkdir(parents=True, exist_ok=True)
	target_path = target_dir / main_path.name
	shutil.copy2(main_path, target_path)
	return str(target_path.relative_to(payload_root))


def copy_includes(
	includes: list[str], payload_root: Path, *, verbose: bool
) -> None:
	for entry in includes:
		src, dest_rel = parse_mapping(entry)
		if not src.is_file():
			raise BuildError(f"Included file is not a file: {src}")
		target_dir = payload_root if dest_rel == "." else payload_root / dest_rel
		target_dir.mkdir(parents=True, exist_ok=True)
		target_path = target_dir / src.name
		log(f"Including file {src} -> {target_path.relative_to(payload_root)}", verbose=verbose)
		shutil.copy2(src, target_path)


def copy_include_folders(
	folders: list[str], payload_root: Path, *, verbose: bool
) -> None:
	for entry in folders:
		src, dest_rel = parse_mapping(entry)
		if not src.is_dir():
			raise BuildError(f"Included folder is not a directory: {src}")
		if dest_rel == ".":
			target_dir = payload_root / src.name
		else:
			target_dir = payload_root / dest_rel
		log(f"Including folder {src} -> {target_dir.relative_to(payload_root)}", verbose=verbose)
		shutil.copytree(src, target_dir, dirs_exist_ok=True)


def copy_asmln_runtime(asmln_exe: Path, payload_root: Path, *, verbose: bool) -> None:
	if not asmln_exe.exists() or not asmln_exe.is_file():
		raise BuildError(f"asmln.exe not found: {asmln_exe}")
	runtime_dir = asmln_exe.parent
	lib_dir = runtime_dir / "lib"
	if not lib_dir.exists() or not lib_dir.is_dir():
		raise BuildError(f"ASM-Lang lib/ folder not found next to {asmln_exe}")

	shutil.copy2(asmln_exe, payload_root / "asmln.exe")
	log("Copied asmln.exe", verbose=verbose)
	shutil.copytree(lib_dir, payload_root / "lib", dirs_exist_ok=True)
	log("Copied lib/", verbose=verbose)


def write_manifest(payload_root: Path, main_rel_path: str) -> None:
	manifest_path = payload_root / MANIFEST_NAME
	manifest_path.write_text(main_rel_path + "\n", encoding="ascii")


def build_payload_zip(payload_root: Path, zip_path: Path) -> None:
	with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
		for fs_path in payload_root.rglob("*"):
			if fs_path.is_dir():
				continue
			arcname = fs_path.relative_to(payload_root)
			zf.write(fs_path, arcname)


def find_csc() -> Path | None:
	windir = Path(os.environ.get("WINDIR", "C:\\Windows"))
	candidates: list[Path] = []
	for framework_root in ["Framework64", "Framework"]:
		base = windir / "Microsoft.NET" / framework_root
		if not base.exists():
			continue
		# Prefer newer versions by sorting descending
		for sub in sorted(base.iterdir(), reverse=True):
			csc_path = sub / "csc.exe"
			if csc_path.exists():
				candidates.append(csc_path)
	return candidates[0] if candidates else None


def compile_stub(temp_dir: Path, *, verbose: bool) -> Path:
	csc_path = find_csc()
	if not csc_path:
		raise BuildError("Could not find csc.exe (part of the .NET Framework) on this system.")

	stub_cs = temp_dir / "stub.cs"
	stub_exe = temp_dir / "stub.exe"
	stub_cs.write_text(STUB_SOURCE, encoding="utf-8")

	cmd = [
		str(csc_path),
		"/nologo",
		"/target:exe",
		"/platform:anycpu",
		f"/out:{stub_exe}",
		"/optimize+",
		"/r:System.IO.Compression.FileSystem.dll",
		str(stub_cs),
	]
	log(f"Compiling stub with {csc_path}", verbose=verbose)
	result = subprocess.run(cmd, capture_output=not verbose, text=True)
	if result.returncode != 0:
		raise BuildError(f"csc.exe failed: {result.stdout}\n{result.stderr}")
	return stub_exe


def assemble_exe(stub_path: Path, payload_zip: Path, output_path: Path) -> None:
	payload_bytes = payload_zip.read_bytes()
	payload_len = len(payload_bytes)

	with stub_path.open("rb") as stub_file, output_path.open("wb") as out:
		shutil.copyfileobj(stub_file, out)
		out.write(payload_bytes)
		out.write(struct.pack("<Q", payload_len))
		out.write(MARKER)


def build(argv: list[str]) -> None:
	args = parse_args(argv)
	ensure_windows()

	asmln_exe = Path(args.asmln_exe).expanduser().resolve()
	main_file = Path(args.main_file).expanduser().resolve()
	main_dest_rel = args.main_dest.strip() or "."
	default_output = Path.cwd() / (main_file.stem + ".exe")
	output_path = Path(args.output).expanduser().resolve() if args.output else default_output.resolve()

	if not main_file.exists() or not main_file.is_file():
		raise BuildError(f"Main script not found: {main_file}")

	with tempfile.TemporaryDirectory() as tmpdir_str:
		tmpdir = Path(tmpdir_str)
		payload_root = tmpdir / "payload"
		payload_root.mkdir(parents=True, exist_ok=True)

		log("Copying ASM-Lang runtime...", verbose=args.verbose)
		copy_asmln_runtime(asmln_exe, payload_root, verbose=args.verbose)

		log("Copying main script...", verbose=args.verbose)
		main_rel_path = copy_main(main_file, main_dest_rel, payload_root)
		log(f"Main placed at {main_rel_path}", verbose=args.verbose)

		if args.include:
			log("Copying additional files...", verbose=args.verbose)
			copy_includes(args.include, payload_root, verbose=args.verbose)

		if args.include_folder:
			log("Copying additional folders...", verbose=args.verbose)
			copy_include_folders(args.include_folder, payload_root, verbose=args.verbose)

		write_manifest(payload_root, main_rel_path)

		payload_zip = tmpdir / "payload.zip"
		build_payload_zip(payload_root, payload_zip)

		stub_exe = compile_stub(tmpdir, verbose=args.verbose)
		output_path.parent.mkdir(parents=True, exist_ok=True)
		assemble_exe(stub_exe, payload_zip, output_path)

		log(f"Built self-extracting EXE: {output_path}", verbose=True)


STUB_SOURCE = r"""
using System;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Text;

internal static class Program
{
	private const string MarkerText = "ASMLSFX1";
	private static readonly byte[] MarkerBytes = Encoding.ASCII.GetBytes(MarkerText);
	private static readonly int FooterLength = MarkerText.Length + 8; // marker + payload length (int64)
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

			string asmlnPath = Path.Combine(tempRoot, "asmln.exe");
			if (!File.Exists(asmlnPath))
				throw new InvalidOperationException("Bundled asmln.exe is missing.");

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
				throw new InvalidOperationException("Failed to start asmln.exe");
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

			fs.Seek(-(FooterLength), SeekOrigin.End);
			long payloadLen = ReadInt64(fs);
			if (payloadLen <= 0 || payloadLen > fs.Length)
				throw new InvalidOperationException("Invalid payload length in footer.");

			long payloadOffset = fs.Length - FooterLength - payloadLen;
			if (payloadOffset < 0)
				throw new InvalidOperationException("Calculated payload offset is invalid.");

			fs.Seek(payloadOffset, SeekOrigin.Begin);
			using (FileStream outFs = new FileStream(zipPath, FileMode.Create, FileAccess.Write, FileShare.None))
			{
				CopyBytes(fs, outFs, payloadLen);
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

	private static void CopyBytes(Stream src, Stream dest, long bytesToCopy)
	{
		byte[] buffer = new byte[8192];
		long remaining = bytesToCopy;
		while (remaining > 0)
		{
			int toRead = (int)Math.Min(buffer.Length, remaining);
			int read = src.Read(buffer, 0, toRead);
			if (read == 0)
				throw new EndOfStreamException("Unexpected end of file while copying payload.");
			dest.Write(buffer, 0, read);
			remaining -= read;
		}
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
"""


if __name__ == "__main__":
	try:
		build(sys.argv[1:])
	except BuildError as exc:
		print(f"Error: {exc}", file=sys.stderr)
		sys.exit(1)
