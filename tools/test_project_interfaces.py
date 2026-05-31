#!/usr/bin/env python3
"""Static interface/build-entry validation for the Cheat Engine repository.

The repository is a native desktop project, not an HTTP API service. This script
therefore treats the public project entrypoints as the interfaces that need
validation: Lazarus projects, Visual Studio solutions/projects, Makefiles, and
the build targets documented in README.md.

It intentionally avoids requiring Lazarus, Visual Studio, GCC, or Make so it can
run in lightweight CI environments as a fast smoke test before heavier builds.
Use --strict-missing-references when you want unresolved project-file references
to fail the command instead of being reported as warnings. This keeps the
default mode suitable for quick repository interface smoke tests.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


@dataclass
class CheckResult:
    name: str
    checked: int = 0
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def fail(self, message: str) -> None:
        self.failures.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)


GUID_RE = re.compile(r'^\{[0-9A-Fa-f-]{36}\}$')
SLN_PROJECT_RE = re.compile(
    r'^Project\("(?P<type_guid>\{[0-9A-Fa-f-]{36}\})"\)\s*=\s*"[^"]+",\s*"(?P<path>[^"]+)",\s*"(?P<guid>\{[0-9A-Fa-f-]{36}\})"'
)
MSBUILD_ITEM_TAGS = {
    'ApplicationDefinition',
    'ClCompile',
    'ClInclude',
    'Compile',
    'Content',
    'CudaCompile',
    'EmbeddedResource',
    'Midl',
    'None',
    'Page',
    'ResourceCompile',
}
XML_PROJECT_EXTENSIONS = {'.vcxproj', '.vcproj', '.csproj', '.lpi'}

README_TARGETS = [
    Path('Cheat Engine/cheatengine.lpi'),
    Path('Cheat Engine/speedhack/speedhack.lpr'),
    Path('Cheat Engine/luaclient/luaclient.lpr'),
    Path('Cheat Engine/Direct x mess/Direct x mess.sln'),
    Path('Cheat Engine/DotNetCompiler/CSCompiler/CSCompiler.sln'),
    Path('Cheat Engine/MonoDataCollector/MonoDataCollector.sln'),
    Path('Cheat Engine/DotNetDataCollector/DotNetDataCollector.sln'),
    Path('Cheat Engine/DotNetInvasiveDataCollector/DotNetInvasiveDataCollector.sln'),
    Path('Cheat Engine/Java/CEJVMTI/CEJVMTI.sln'),
    Path('Cheat Engine/tcclib/win32/tcc/tcc.sln'),
    Path('Cheat Engine/VEHDebug/vehdebug.lpr'),
    Path('DBKKernel/DBKKernel.sln'),
]


class InterfaceValidator:
    def __init__(self, root: Path, strict_missing_references: bool = False, verbose: bool = False) -> None:
        self.root = root.resolve()
        self.strict_missing_references = strict_missing_references
        self.verbose = verbose
        self.tracked_files = self._git_ls_files()
        self.tracked_posix = {p.as_posix().lower() for p in self.tracked_files}
        self.results: list[CheckResult] = []

    def _git_ls_files(self) -> list[Path]:
        try:
            completed = subprocess.run(
                ['git', 'ls-files', '-z'],
                cwd=self.root,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            raise RuntimeError(f'Unable to list tracked files with git: {exc}') from exc

        files: list[Path] = []
        for raw in completed.stdout.split(b'\0'):
            if not raw:
                continue
            files.append(Path(raw.decode('utf-8', errors='replace')))
        return files

    def _tracked_by_suffix(self, *suffixes: str) -> list[Path]:
        wanted = {s.lower() for s in suffixes}
        return sorted([p for p in self.tracked_files if p.suffix.lower() in wanted], key=lambda x: x.as_posix().lower())

    def _path_exists_case_insensitive(self, path: Path | str) -> bool:
        candidate = Path(path)
        rel = candidate.as_posix().lower()
        return rel in self.tracked_posix or (self.root / candidate).exists()

    @staticmethod
    def _local_name(tag: str) -> str:
        return tag.rsplit('}', 1)[-1]

    @staticmethod
    def _looks_like_external(value: str) -> bool:
        normalized = value.replace('\\', '/')
        return (
            not value
            or '$(' in value
            or '%' in value
            or normalized.startswith(('http://', 'https://'))
            or re.match(r'^[A-Za-z]:/', normalized) is not None
            or normalized.startswith(('/', '#'))
            or GUID_RE.match(value) is not None
        )

    @staticmethod
    def _looks_like_file_reference(value: str) -> bool:
        normalized = value.replace('\\', '/')
        basename = normalized.rsplit('/', 1)[-1]
        return '.' in basename

    @staticmethod
    def _split_search_path(value: str) -> list[str]:
        return [item.strip() for item in value.split(';') if item.strip()]

    def _reference_candidates(self, base_file: Path, value: str, search_paths: Iterable[str] = ()) -> list[Path]:
        normalized = value.replace('\\', '/')
        candidates = [Path((base_file.parent / normalized).as_posix())]
        for search_path in search_paths:
            if self._looks_like_external(search_path):
                continue
            search_normalized = search_path.replace('\\', '/')
            candidates.append(Path((base_file.parent / search_normalized / normalized).as_posix()))
        return candidates

    def _reference_exists(self, base_file: Path, value: str, search_paths: Iterable[str] = ()) -> bool:
        return any(self._path_exists_case_insensitive(candidate) for candidate in self._reference_candidates(base_file, value, search_paths))

    def _missing_reference(self, result: CheckResult, message: str) -> None:
        if self.strict_missing_references:
            result.fail(message)
        else:
            result.warn(message)

    def run(self) -> int:
        self.results = [
            self.check_core_repository_shape(),
            self.check_readme_targets(),
            self.check_xml_projects_parse(),
            self.check_lazarus_project_units(),
            self.check_solution_references(),
            self.check_msbuild_file_references(),
            self.check_makefiles(),
            self.check_appveyor_build_entrypoint(),
        ]
        self.print_report()
        return 1 if any(result.failures for result in self.results) else 0

    def check_core_repository_shape(self) -> CheckResult:
        result = CheckResult('core repository shape')
        required = [
            Path('README.md'),
            Path('appveyor.yml'),
            Path('Cheat Engine'),
            Path('DBKKernel'),
            Path('dbvm'),
            Path('lua'),
        ]
        for path in required:
            result.checked += 1
            if not (self.root / path).exists():
                result.fail(f'missing required path: {path.as_posix()}')
        if (self.root / '.github/workflows').exists():
            result.warn('GitHub Actions workflows exist; check them separately if CI behavior is being changed')
        return result

    def check_readme_targets(self) -> CheckResult:
        result = CheckResult('README build targets')
        for path in README_TARGETS:
            result.checked += 1
            if not self._path_exists_case_insensitive(path):
                result.fail(f'README build target is missing: {path.as_posix()}')
        return result

    def check_xml_projects_parse(self) -> CheckResult:
        result = CheckResult('XML project files parse')
        for path in self._tracked_by_suffix(*XML_PROJECT_EXTENSIONS):
            result.checked += 1
            try:
                ET.parse(self.root / path)
            except ET.ParseError as exc:
                result.fail(f'{path.as_posix()}: XML parse error: {exc}')
        return result

    def _lazarus_search_paths(self, tree: ET.ElementTree) -> list[str]:
        search_paths: list[str] = []
        for elem in tree.iter():
            if self._local_name(elem.tag) not in {'OtherUnitFiles', 'IncludeFiles'}:
                continue
            value = elem.attrib.get('Value', '').strip()
            search_paths.extend(self._split_search_path(value))
        return search_paths

    def check_lazarus_project_units(self) -> CheckResult:
        result = CheckResult('Lazarus .lpi unit references')
        for path in self._tracked_by_suffix('.lpi'):
            result.checked += 1
            try:
                tree = ET.parse(self.root / path)
            except ET.ParseError:
                # The XML parser check reports this once.
                continue
            search_paths = self._lazarus_search_paths(tree)
            for units_node in tree.findall('.//Units'):
                for unit_node in list(units_node):
                    filename_node = unit_node.find('Filename')
                    if filename_node is None:
                        continue
                    filename = filename_node.attrib.get('Value', '').strip()
                    if self._looks_like_external(filename) or not self._looks_like_file_reference(filename):
                        continue
                    if not self._reference_exists(path, filename, search_paths):
                        self._missing_reference(result, f'{path.as_posix()}: unresolved Lazarus unit reference {filename}')
        return result

    def check_solution_references(self) -> CheckResult:
        result = CheckResult('Visual Studio solution references')
        for path in self._tracked_by_suffix('.sln'):
            result.checked += 1
            text = (self.root / path).read_text(encoding='utf-8', errors='replace')
            for line in text.splitlines():
                match = SLN_PROJECT_RE.match(line.strip())
                if not match:
                    continue
                project_path = match.group('path').strip()
                if self._looks_like_external(project_path):
                    continue
                # Solution folders use a GUID-like path rather than a project file path.
                if not self._looks_like_file_reference(project_path):
                    continue
                if self._reference_exists(path, project_path):
                    continue
                legacy_candidate = None
                if project_path.lower().endswith('.vcxproj'):
                    legacy_candidate = project_path[:-8] + '.vcproj'
                if legacy_candidate and self._reference_exists(path, legacy_candidate):
                    result.warn(f'{path.as_posix()}: solution references {project_path}, but legacy {legacy_candidate} exists')
                    continue
                self._missing_reference(result, f'{path.as_posix()}: unresolved project reference {project_path}')
        return result

    def check_msbuild_file_references(self) -> CheckResult:
        result = CheckResult('MSBuild project file references')
        for path in self._tracked_by_suffix('.vcxproj', '.vcproj', '.csproj'):
            result.checked += 1
            try:
                tree = ET.parse(self.root / path)
            except ET.ParseError:
                continue
            for elem in tree.iter():
                tag = self._local_name(elem.tag)
                if tag not in MSBUILD_ITEM_TAGS:
                    continue
                include = elem.attrib.get('Include') or elem.attrib.get('Update')
                if not include:
                    continue
                for value in include.split(';'):
                    value = value.strip()
                    if self._looks_like_external(value) or not self._looks_like_file_reference(value):
                        continue
                    # Framework references such as System.Net.Http are not files.
                    if '/' not in value.replace('\\', '/') and Path(value).suffix.lower() == '':
                        continue
                    if not self._reference_exists(path, value):
                        self._missing_reference(result, f'{path.as_posix()}: unresolved MSBuild file reference {value}')
        return result

    def check_makefiles(self) -> CheckResult:
        result = CheckResult('Makefile entrypoints')
        makefiles = [
            p for p in self.tracked_files
            if p.name.lower() == 'makefile' or p.suffix.lower() in {'.mak', '.mk'}
        ]
        result.checked = len(makefiles)
        if not makefiles:
            result.fail('no Makefile or .mak/.mk entrypoints found')
        return result

    def check_appveyor_build_entrypoint(self) -> CheckResult:
        result = CheckResult('AppVeyor build entrypoint')
        appveyor = self.root / 'appveyor.yml'
        result.checked = 1
        if not appveyor.exists():
            result.fail('appveyor.yml is missing')
            return result
        text = appveyor.read_text(encoding='utf-8', errors='replace')
        if 'lazbuild' not in text.lower():
            result.fail('appveyor.yml does not invoke lazbuild for Lazarus projects')
        if re.search(r'^\s*test:\s*off\s*$', text, flags=re.IGNORECASE | re.MULTILINE):
            result.warn('appveyor.yml has test: off; this script only provides static smoke validation')
        return result

    def print_report(self) -> None:
        mode = 'strict' if self.strict_missing_references else 'default'
        print('Static project/interface validation report')
        print(f'Repository: {self.root}')
        print(f'Mode: {mode}')
        print(f'Tracked files: {len(self.tracked_files)}')
        print('')
        total_checked = 0
        total_failures = 0
        total_warnings = 0
        for result in self.results:
            total_checked += result.checked
            total_failures += len(result.failures)
            total_warnings += len(result.warnings)
            status = 'PASS' if not result.failures else 'FAIL'
            print(f'[{status}] {result.name}: checked={result.checked}, failures={len(result.failures)}, warnings={len(result.warnings)}')
            details: Iterable[tuple[str, str]] = [*(('FAIL', f) for f in result.failures), *(('WARN', w) for w in result.warnings)]
            for level, message in details:
                print(f'  - {level}: {message}')
        print('')
        print(f'Summary: checked={total_checked}, failures={total_failures}, warnings={total_warnings}')


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1], help='repository root')
    parser.add_argument('--strict-missing-references', action='store_true', help='fail on unresolved project-file references')
    parser.add_argument('--verbose', action='store_true', help='print verbose diagnostics')
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        return InterfaceValidator(
            args.root,
            strict_missing_references=args.strict_missing_references,
            verbose=args.verbose,
        ).run()
    except RuntimeError as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
