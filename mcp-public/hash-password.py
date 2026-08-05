import sys

import bcrypt


password = sys.stdin.buffer.readline().rstrip(b"\r\n")
if not password:
    raise SystemExit("password is empty")
sys.stdout.write(bcrypt.hashpw(password, bcrypt.gensalt(rounds=12)).decode("ascii"))
