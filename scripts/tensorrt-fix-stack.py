"""Clear obsolete executable-stack ELF flags in TensorRT 10.3 builder libraries.

Required with glibc 2.41; does not relax host/container stack protections.
Equivalent to patchelf --clear-execstack for these ELF64 little-endian files.
"""
from pathlib import Path
import struct
import sysconfig

root = Path(sysconfig.get_paths()["purelib"]) / "tensorrt_libs"
for path in root.glob("libnvinfer_builder_resource*.so.*"):
    with path.open("r+b") as f:
        header = f.read(64)
        if header[:6] != b"\x7fELF\x02\x01":
            raise RuntimeError(f"Unsupported ELF format: {path}")
        offset = struct.unpack_from("<Q", header, 32)[0]
        size, count = struct.unpack_from("<HH", header, 54)
        for index in range(count):
            pos = offset + index * size
            f.seek(pos)
            kind, flags = struct.unpack("<II", f.read(8))
            if kind == 0x6474E551 and flags & 1:
                f.seek(pos + 4)
                f.write(struct.pack("<I", flags & ~1))
                print("Cleared executable stack:", path.name)
