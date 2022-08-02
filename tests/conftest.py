# Copyright (C) 2017 Mandiant, Inc. All Rights Reserved.

import os

import yaml
import pytest
import viv_utils

import floss.main as floss_main
import floss.stackstrings as stackstrings
from floss.identify import get_top_functions, find_decoding_function_features


def extract_strings(vw):
    """
    Deobfuscate strings from vivisect workspace
    """
    decoding_functions_candidates = identify_decoding_functions(vw)
    ret = [
        s.string
        for s in floss_main.decode_strings(
            vw, decoding_functions_candidates, 4, disable_progress=True
        )
    ]

    selected_functions = floss_main.select_functions(vw, None)
    ret.extend(
        s.string
        for s in stackstrings.extract_stackstrings(
            vw, selected_functions, 4, quiet=True
        )
    )

    return ret


def identify_decoding_functions(vw):
    selected_functions = floss_main.select_functions(vw, None)
    decoding_function_features, _ = find_decoding_function_features(vw, list(selected_functions), disable_progress=True)
    top_functions = get_top_functions(decoding_function_features, 20)
    return list(map(lambda p: p[0], top_functions))


def pytest_collect_file(parent, path):
    if path.basename == "test.yml":
        return YamlFile.from_parent(parent, fspath=path)


class YamlFile(pytest.File):
    def collect(self):
        spec = yaml.safe_load(self.fspath.open())
        test_dir = os.path.dirname(str(self.fspath))
        # TODO specify max runtime via command line option
        MAX_RUNTIME = 30.0
        for platform, archs in spec["Output Files"].items():
            for arch, filename in archs.items():
                try:
                    runtime_raw = spec["FLOSS running time"]
                    runtime = float(runtime_raw.split(" ")[0])
                    if runtime > MAX_RUNTIME:
                        # skip this test
                        continue
                except (KeyError, ValueError):
                    pass
                filepath = os.path.join(test_dir, filename)
                if os.path.exists(filepath):
                    yield FLOSSTest.from_parent(
                        self, path=filepath, platform=platform, arch=arch, filename=filename, spec=spec
                    )


class FLOSSTestError(Exception):
    def __init__(self, expected, got):
        self.expected = expected
        self.got = got


class FLOSSStringsNotExtracted(FLOSSTestError):
    pass


class FLOSSDecodingFunctionNotFound(Exception):
    pass


class FLOSSTest(pytest.Item):
    def __init__(self, parent, path, platform, arch, filename, spec):
        name = "{name:s}::{platform:s}::{arch:s}".format(name=spec["Test Name"], platform=platform, arch=arch)
        super(FLOSSTest, self).__init__(name, parent)
        self.spec = spec
        self.platform = platform
        self.arch = arch
        self.filename = filename

    def _test_strings(self, test_path):
        expected_strings = set(self.spec["Decoded strings"])
        if not expected_strings:
            return

        arch = self.spec.get("Shellcode Architecture")
        if arch in ("i386", "amd64"):
            vw = viv_utils.getShellcodeWorkspaceFromFile(test_path, arch)
        else:
            # default assumes pe
            vw = viv_utils.getWorkspace(test_path)
        found_strings = set(extract_strings(vw))
        if not (expected_strings <= found_strings):
            raise FLOSSStringsNotExtracted(expected_strings, found_strings)

    def _test_detection(self, test_path):
        try:
            expected_functions = set(self.spec["Decoding routines"][self.platform][self.arch])
        except KeyError:
            expected_functions = set([])

        if not expected_functions:
            return

        vw = viv_utils.getWorkspace(test_path)
        found_functions = set(identify_decoding_functions(vw))

        if not (expected_functions <= found_functions):
            raise FLOSSDecodingFunctionNotFound(expected_functions, found_functions)

    def runtest(self):
        xfail = self.spec.get("Xfail", {})
        if "all" in xfail:
            pytest.xfail("unsupported test case (known issue)")

        if "{0.platform:s}-{0.arch:s}".format(self) in xfail:
            pytest.xfail("unsupported platform&arch test case (known issue)")

        spec_path = self.location[0]
        test_dir = os.path.dirname(spec_path)
        test_path = os.path.join(test_dir, self.filename)

        self._test_detection(test_path)
        self._test_strings(test_path)

    def reportinfo(self):
        return self.fspath, 0, f"usecase: {self.name}"

    def repr_failure(self, excinfo):
        if isinstance(excinfo.value, FLOSSStringsNotExtracted):
            expected = excinfo.value.expected
            got = excinfo.value.got
            return "\n".join(
                [
                    "FLOSS extraction failed:",
                    f"   expected: {str(expected)}",
                    f"   got: {str(got)}",
                    f"   expected-got: {str(set(expected) - set(got))}",
                    f"   got-expected: {str(set(got) - set(expected))}",
                ]
            )
