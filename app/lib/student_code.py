import random

CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def gen_student_code() -> str:
    return "ST" + "".join(random.choice(CODE_CHARS) for _ in range(6))
