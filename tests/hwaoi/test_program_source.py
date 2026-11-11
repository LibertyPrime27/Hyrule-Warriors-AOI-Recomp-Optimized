"""Execute the production source codec and registration with authored bytes.

Checks ownership, concurrent expansion, alias replacement, checksum rejection,
optional allocation failures and incompressible/multi-stage fallbacks. No GPU,
game input, or generated guest code is required.
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
CC = Path(os.environ.get('HWAOI_TOOLS', 'F:') + '/llvm-mingw/bin/clang.exe')
DEPS = Path(os.environ.get('HWAOI_TEST_DEPS') or 'F:/TotKDependencies/runtime/deps')
sys.path.insert(0, str(ROOT / 'tests'))
from nvn2_compute_c0_test import function


def source(omit_prefetch_expansion=False):
    obj = (ROOT / 'runtime/nvn_api_obj.c').read_text(encoding='utf-8')
    shader = (ROOT / 'runtime/gfx_shader.c').read_text(encoding='utf-8')
    draw = (ROOT / 'runtime/vk_exec_draw.c').read_text(encoding='utf-8')
    fixture = (ROOT / 'tests/hwaoi/program_source_fixture.c').read_text(encoding='utf-8')
    actual = '\n'.join((function(shader, 'int gfx_shader_is_nvn2('),
                        function(shader, 'int gfx_shader_nvn2_sizes('),
                        function(obj, 'static void prog_release('),
                        function(obj, 'static int prog_set_shaders(')))
    prefetch = function(draw, 'void vk_exec_program_registered(')
    if omit_prefetch_expansion:
        assert prefetch.count('code = nvn_program_source(code);') == 1
        prefetch = prefetch.replace('code = nvn_program_source(code);', '')
    prefetch = function(draw, 'static int program_stage_info(') + '\n' + prefetch
    return fixture.replace('@SOURCE_CODEC@', (ROOT / 'runtime/nvn_program_source.c').read_text(encoding='utf-8')).replace('@PROGRAM_PREFETCH@', prefetch).replace('@PROGRAM_FUNCTIONS@', actual)


@unittest.skipUnless(os.name == 'nt' and CC.is_file() and (DEPS/'lib/libzstd.a').is_file(), 'Windows Clang and zstd required')
class ProgramSourceTests(unittest.TestCase):
    def test_real_codec_ownership_concurrency_and_rejection(self):
        with tempfile.TemporaryDirectory(prefix='hwaoi-program-source-') as folder:
            directory = Path(folder)
            path = directory/'fixture.c'
            path.write_text(source(), encoding='utf-8')
            for optimization in ('-O0', '-O2'):
                exe = directory/'fixture.exe'
                compiled = subprocess.run([str(CC), optimization, '-Wall', '-Wextra', '-Werror',
                    '-Wno-unused-function', '-Wno-missing-field-initializers', '-I', str(ROOT/'runtime'),
                    '-I', str(DEPS/'include'), str(path), str(DEPS/'lib/libzstd.a'), '-o', str(exe)],
                    capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60)
                self.assertEqual(compiled.returncode, 0, compiled.stdout+compiled.stderr)
                for mode in ('on', 'off', 'corrupt', 'expand-oom', 'prefetch', 'prefetch-fail',
                             'prefetch-off', 'prefetch-sync', 'prefetch-graphics'):
                    with self.subTest(optimization=optimization, mode=mode):
                        run = subprocess.run([str(exe), mode], capture_output=True, text=True,
                                             encoding='utf-8', errors='replace', timeout=30)
                        self.assertEqual(run.returncode, 73 if mode in ('corrupt','expand-oom') else 0, run.stdout+run.stderr)
                        if mode == 'corrupt':
                            self.assertIn('checksum', run.stderr)
                        elif mode == 'expand-oom':
                            self.assertIn('expansion allocation failed', run.stderr)
                path.write_text(source(omit_prefetch_expansion=True), encoding='utf-8')
                compiled = subprocess.run([str(CC), optimization, '-Wall', '-Wextra', '-Werror',
                    '-Wno-unused-function', '-Wno-missing-field-initializers', '-I', str(ROOT/'runtime'),
                    '-I', str(DEPS/'include'), str(path), str(DEPS/'lib/libzstd.a'), '-o', str(exe)],
                    capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60)
                self.assertEqual(compiled.returncode, 0, compiled.stdout+compiled.stderr)
                rejected = subprocess.run([str(exe), 'prefetch'], capture_output=True, text=True, timeout=30)
                self.assertEqual(rejected.returncode, 1, rejected.stdout+rejected.stderr)
                self.assertIn('complete_allocation', rejected.stderr)
                path.write_text(source(), encoding='utf-8')


if __name__ == '__main__':
    unittest.main()
