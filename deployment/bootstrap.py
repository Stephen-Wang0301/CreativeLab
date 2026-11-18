"""Install fixed dependencies after a fresh Git clone; leave existing data intact."""

from pathlib import Path
import subprocess
import sys

BASE = Path(__file__).resolve().parents[1]


def main():
    if sys.version_info < (3, 11):
        raise RuntimeError('Use Python 3.12 or newer.')
    packages = BASE / 'runtime/packages'
    if not packages.exists():
        subprocess.run([sys.executable, '-m', 'pip', 'install', '--target', str(packages),
                        '-r', str(BASE / 'requirements.txt')], check=True)
    elif not (packages / 'flask/__init__.py').is_file() or not (packages / 'waitress/__init__.py').is_file():
        raise RuntimeError('Existing runtime/packages is incomplete; inspect it before replacing dependencies.')
    private_readme = BASE / 'README.md'
    if not private_readme.exists():
        private_readme.write_bytes((BASE / '.github/README.md').read_bytes())
    subprocess.run(['git', '-C', str(BASE), 'config', '--local', 'core.hooksPath', '.githooks'], check=True)
    print('Dependencies and private README are ready. Existing data and private settings were preserved.')


if __name__ == '__main__':
    main()
