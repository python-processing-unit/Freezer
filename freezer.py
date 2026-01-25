"""
Executable builder for Prefix on Windows 10+.

Creates a self-extracting executable that bundles:
- The Prefix runtime (`prefix.exe`) and its `lib/` folder.
- A main Prefix script to run on startup.
- Optional additional files and folders placed at custom paths inside the bundle.

Runtime behavior of the generated EXE:
1) Extracts itself to a temp directory.
2) Runs `prefix.exe <main script>` from that temp directory.
3) Cleans up extracted files on exit.

No external dependencies are required beyond stock Windows 10 (uses the built-in
`csc.exe` compiler from the .NET Framework). A working Prefix installation is
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
import hashlib
from pathlib import Path


MARKER = b"PREFIXSFX1"
# footer layout: payload length (int64 LE) + SHA256 (32 bytes) + marker
FOOTER_LEN = len(MARKER) + 8 + 32
MANIFEST_NAME = "__main_path.txt"


class BuildError(Exception):
	"""Raised for predictable build-time failures."""


def parse_args(argv: list[str]) -> argparse.Namespace:
	parser = argparse.ArgumentParser(
		description="Build a self-extracting prefix executable for Windows 10+",
		formatter_class=argparse.ArgumentDefaultsHelpFormatter,
	)

	parser.add_argument(
		"pre_exe",
		nargs="?",
		default=None,
		help="Path to the prefix prefix.exe to bundle (optional; will search PATH if omitted)",
	)
	parser.add_argument(
		"main_file",
		help="Path to the main prefix script to run on startup",
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

	parser.add_argument(
		"--icon",
		default=None,
		help="Path to an ICO or image file to use as the generated EXE icon; non-ICO images will be converted using GDI+.",
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


def copy_pre_runtime(pre_exe: Path, payload_root: Path, *, verbose: bool) -> None:
	if not pre_exe.exists() or not pre_exe.is_file():
		raise BuildError(f"prefix.exe not found: {pre_exe}")
	runtime_dir = pre_exe.parent

	# Copy all files and folders from the runtime directory into the payload root.
	# This ensures the entire runtime directory (exe, lib/, ext/, etc.) is embedded.
	for entry in sorted(runtime_dir.iterdir()):
		# Skip Git metadata and ignore files that shouldn't be bundled.
		if entry.name in (".git", ".gitignore"):
			log(f"Skipping {entry.name}", verbose=verbose)
			continue
		target = payload_root / entry.name
		if entry.is_dir():
			shutil.copytree(entry, target, dirs_exist_ok=True)
			log(f"Copied runtime directory {entry.name}/ -> {target.relative_to(payload_root)}", verbose=verbose)
		else:
			shutil.copy2(entry, target)
			log(f"Copied runtime file {entry.name} -> {target.relative_to(payload_root)}", verbose=verbose)


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



def compile_stub(temp_dir: Path, *, verbose: bool, win_icon: Path | None = None) -> Path:
	# Backwards-compatible wrapper that optionally compiles the stub with an icon.
	csc_path = find_csc()
	if not csc_path:
		raise BuildError("Could not find csc.exe (part of the .NET Framework) on this system.")

	stub_cs = temp_dir / "stub.cs"
	stub_exe = temp_dir / "stub.exe"
	stub_source_path = Path(__file__).parent / "bootloader.cs"
	if not stub_source_path.exists():
		raise BuildError(f"Embedded C# stub file not found: {stub_source_path}")
	stub_cs.write_text(stub_source_path.read_text(encoding="utf-8"), encoding="utf-8")

	cmd = [
		str(csc_path),
		"/nologo",
		"/target:exe",
		"/platform:anycpu",
		f"/out:{stub_exe}",
		"/optimize+",
		"/r:System.IO.Compression.FileSystem.dll",
	]
	if win_icon:
		cmd.append(f"/win32icon:{win_icon}")
	cmd.append(str(stub_cs))

	log(f"Compiling stub with {csc_path}", verbose=verbose)
	result = subprocess.run(cmd, capture_output=not verbose, text=True)
	if result.returncode != 0:
		raise BuildError(f"csc.exe failed: {result.stdout}\n{result.stderr}")
	return stub_exe


def convert_image_to_ico(icon_path: Path, tmp_dir: Path, *, verbose: bool) -> Path:
	icon_path = icon_path.expanduser().resolve()
	if not icon_path.exists():
		raise BuildError(f"Icon file not found: {icon_path}")
	if icon_path.suffix.lower() == ".ico":
		return icon_path

	csc_path = find_csc()
	if not csc_path:
		raise BuildError("Could not find csc.exe to compile the image-to-ico converter.")

	converter_cs = tmp_dir / "gen_ico.cs"
	converter_exe = tmp_dir / "gen_ico.exe"
	out_ico = tmp_dir / "icon.ico"

	source_path = Path(__file__).parent / "gen_ico.cs"
	if not source_path.exists():
		raise BuildError(f"Image converter source not found: {source_path}")

	cmd = [
		str(csc_path),
		"/nologo",
		"/target:exe",
		f"/out:{converter_exe}",
		"/optimize+",
		"/r:System.Drawing.dll",
		str(source_path),
	]
	log(f"Compiling image->ICO converter with {csc_path}", verbose=verbose)
	res = subprocess.run(cmd, capture_output=not verbose, text=True)
	if res.returncode != 0:
		raise BuildError(f"Failed to compile image converter: {res.stdout}\n{res.stderr}")

	# Run the converter
	run = subprocess.run([str(converter_exe), str(icon_path), str(out_ico)], capture_output=not verbose, text=True)
	if run.returncode != 0:
		raise BuildError(f"Image converter failed: {run.stdout}\n{run.stderr}")
	if not out_ico.exists():
		raise BuildError("Image converter did not produce an ICO file.")
	log(f"Converted {icon_path} -> {out_ico}", verbose=verbose)
	return out_ico


def assemble_exe(stub_path: Path, payload_zip: Path, output_path: Path) -> None:
	payload_bytes = payload_zip.read_bytes()
	payload_len = len(payload_bytes)
	sha256_digest = hashlib.sha256(payload_bytes).digest()

	with stub_path.open("rb") as stub_file, output_path.open("wb") as out:
		shutil.copyfileobj(stub_file, out)
		out.write(payload_bytes)
		out.write(struct.pack("<Q", payload_len))
		out.write(sha256_digest)
		out.write(MARKER)


def build(argv: list[str]) -> None:
	args = parse_args(argv)
	ensure_windows()

	if args.pre_exe is None:
		# Try to find prefix.exe on PATH when omitted
		found = shutil.which("prefix.exe") or shutil.which("prefix")
		if not found:
			raise BuildError(
				"prefix.exe not provided and not found on PATH; provide its path or install prefix."
			)
		pre_exe = Path(found).expanduser().resolve()
		log(f"Found prefix.exe on PATH: {pre_exe}", verbose=args.verbose)
	else:
		pre_exe = Path(args.pre_exe).expanduser().resolve()
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

		log("Copying prefix runtime...", verbose=args.verbose)
		copy_pre_runtime(pre_exe, payload_root, verbose=args.verbose)

		log("Copying main script...", verbose=args.verbose)
		main_rel_path = copy_main(main_file, main_dest_rel, payload_root)
		log(f"Main placed at {main_rel_path}", verbose=args.verbose)

		# Ensure an extension pointer file (.prex) is present in the bundle so
		# the runtime can discover included `ext/` modules automatically when
		# the SFX extracts and runs from the temp directory.
		# Prefer an existing pointer file next to the original main file, or
		# the program-specific .prex (e.g. program.prex). Otherwise generate
		# one that points into the bundled `ext/` folder.
		try:
			pointer_candidates = [
				main_file.parent / ".prex",
				main_file.with_suffix(".prex"),
			]
			pointer_copied = False
			for pc in pointer_candidates:
				if pc.exists():
					shutil.copy2(pc, payload_root / ".prex")
					log(f"Copied pointer file {pc} -> .prex", verbose=args.verbose)
					pointer_copied = True
					break
			if not pointer_copied:
				# Auto-generate .prex listing all .py files in payload_root/ext
				ext_dir = payload_root / "ext"
				if ext_dir.exists() and ext_dir.is_dir():
					lines: list[str] = []
					for f in sorted(ext_dir.iterdir()):
						if f.is_file() and f.suffix == ".py":
							# Reference the file relative to the bundle root so the
							# interpreter will resolve it directly after extraction.
							lines.append(str(Path("ext") / f.name))
					if lines:
						(payload_root / ".prex").write_text("\n".join(lines) + "\n", encoding="utf-8")
						log(f"Generated .prex with {len(lines)} extensions", verbose=args.verbose)
		except OSError:
			# Best-effort only; failure to copy/generate a pointer should not
			# abort the build. The runtime will continue without extensions.
			pass

		if args.include:
			log("Copying additional files...", verbose=args.verbose)
			copy_includes(args.include, payload_root, verbose=args.verbose)

		if args.include_folder:
			log("Copying additional folders...", verbose=args.verbose)
			copy_include_folders(args.include_folder, payload_root, verbose=args.verbose)

		write_manifest(payload_root, main_rel_path)

		payload_zip = tmpdir / "payload.zip"
		build_payload_zip(payload_root, payload_zip)

		# If an icon was provided, convert it to ICO (if needed) and compile the stub with it.
		ico_for_build = None
		if args.icon:
			ico_for_build = convert_image_to_ico(Path(args.icon), tmpdir, verbose=args.verbose)
		stub_exe = compile_stub(tmpdir, verbose=args.verbose, win_icon=ico_for_build)
		output_path.parent.mkdir(parents=True, exist_ok=True)
		assemble_exe(stub_exe, payload_zip, output_path)

		log(f"Built self-extracting EXE: {output_path}", verbose=True)


if __name__ == "__main__":
	try:
		build(sys.argv[1:])
	except BuildError as exc:
		print(f"Error: {exc}", file=sys.stderr)
		sys.exit(1)
