"""Increment file version once per packaging/build, never on application launch."""
from pathlib import Path
import re

def increment():
    path = Path(__file__).with_name('version.py')
    source = path.read_text(encoding='utf-8')
    match = re.search(r'(?m)^BUILD_NUMBER = (\d+)$', source)
    if not match:
        raise RuntimeError('BUILD_NUMBER not found')
    number = int(match.group(1)) + 1
    path.write_text(source[:match.start(1)] + str(number) + source[match.end(1):], encoding='utf-8')
    return number

if __name__ == '__main__':
    print('New build number:', increment())
