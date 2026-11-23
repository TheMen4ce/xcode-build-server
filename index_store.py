"""
Tells sourcekit-lsp which units of the index store are current (BSP extension `outputPathsProvider`).

Xcode keeps one unit per source file and output file, e.g. one for the last device and one for the last simulator
build of a file, plus the units of files that were deleted since. Without output paths, sourcekit-lsp merges all units
of a file, so an outdated unit yields a second definition or one on a wrong line. With output paths, it only uses the
units whose output paths `buildTarget/sources` reports: here the most recently written unit of each source file, which
covers builds as well as Xcode's background indexing.
"""

import ctypes
import logging
import os
from typing import Dict, Optional, Tuple

from compile_database import getXcodeBasePath
from misc import get_mtime

logger = logging.getLogger(__name__)


class _StringRef(ctypes.Structure):
    """indexstore_string_ref_t"""

    _fields_ = [("data", ctypes.c_void_p), ("length", ctypes.c_size_t)]

    def __str__(self):
        return ctypes.string_at(self.data, self.length).decode("utf-8", "replace") if self.data else ""


def _load_library():
    """libIndexStore of the selected Xcode, the library sourcekit-lsp reads the index store with"""
    lib = ctypes.CDLL(
        os.path.join(getXcodeBasePath(), "Toolchains/XcodeDefault.xctoolchain/usr/lib/libIndexStore.dylib")
    )
    p, error_p = ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)
    for name, restype, argtypes in [
        ("indexstore_format_version", ctypes.c_uint, []),
        ("indexstore_error_dispose", None, [p]),
        ("indexstore_store_create", p, [ctypes.c_char_p, error_p]),
        ("indexstore_unit_reader_create", p, [p, ctypes.c_char_p, error_p]),
        ("indexstore_unit_reader_dispose", None, [p]),
        ("indexstore_unit_reader_has_main_file", ctypes.c_bool, [p]),
        ("indexstore_unit_reader_get_main_file", _StringRef, [p]),
        ("indexstore_unit_reader_get_output_file", _StringRef, [p]),
    ]:
        function = getattr(lib, name)
        function.restype = restype
        function.argtypes = argtypes
    return lib


class IndexStore:
    """Scans the units of an index store incrementally: only units written since the last scan are read"""

    @staticmethod
    def open(path: str) -> Optional["IndexStore"]:
        try:
            return IndexStore(_load_library(), path)
        except (OSError, AttributeError) as e:
            logger.warning(f"can't load libIndexStore, so sourcekit-lsp uses all units of the index store: {e}")
            return None

    def __init__(self, lib, path: str):
        self.lib = lib
        self.path = path
        self.units_path = os.path.join(path, f"v{lib.indexstore_format_version()}", "units")
        # created on the first scan, since the index store may only appear with the first build
        self.store = None
        # {unit name: (mtime, main file, output path)}
        # main file and output path are None for module units and unreadable units
        self.units: Dict[str, Tuple[float, Optional[str], Optional[str]]] = {}

    @property
    def mtime(self) -> float:
        """changes whenever a unit is written, since units are written to a temporary file and renamed into place"""
        return get_mtime(self.units_path)

    def output_paths(self) -> Dict[str, str]:
        """{source file: output path of its most recently written unit}"""
        self._scan()
        newest: Dict[str, Tuple[float, str]] = {}
        for mtime, main_file, output_path in self.units.values():
            if main_file and output_path and (main_file not in newest or newest[main_file][0] < mtime):
                newest[main_file] = (mtime, output_path)
        return {main_file: output_path for main_file, (_, output_path) in newest.items()}

    def _scan(self):
        if not os.path.isdir(self.units_path):
            self.units = {}
            return
        if not self.store:
            self.store = self._call("indexstore_store_create", self.path.encode())
            if not self.store:
                return

        units = {}
        with os.scandir(self.units_path) as entries:
            for entry in entries:
                try:
                    mtime = entry.stat().st_mtime
                except FileNotFoundError:  # deleted while scanning
                    continue
                cached = self.units.get(entry.name)
                units[entry.name] = cached if cached and cached[0] == mtime else (mtime, *self._read_unit(entry.name))
        self.units = units

    def _read_unit(self, name: str) -> Tuple[Optional[str], Optional[str]]:
        """(main file, output path) of a unit"""
        reader = self._call("indexstore_unit_reader_create", self.store, name.encode())
        if not reader:
            return None, None
        try:
            if not self.lib.indexstore_unit_reader_has_main_file(reader):
                return None, None
            return (
                str(self.lib.indexstore_unit_reader_get_main_file(reader)),
                str(self.lib.indexstore_unit_reader_get_output_file(reader)),
            )
        finally:
            self.lib.indexstore_unit_reader_dispose(reader)

    def _call(self, name: str, *args):
        """calls a libIndexStore function that reports failures through a trailing indexstore_error_t out parameter"""
        error = ctypes.c_void_p()
        result = getattr(self.lib, name)(*args, ctypes.byref(error))
        if error:
            self.lib.indexstore_error_dispose(error)
        return result
