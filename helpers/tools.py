import secrets
import string

_MD_SPECIAL = r"\*_~`|>"


def generate_secure_string(length: int = 8) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def escape_md(text: str) -> str:
    for ch in _MD_SPECIAL:
        text = text.replace(ch, "\\" + ch)
    return text


def command_mention(name: str, command_id: int) -> str:
    return f"</{name}:{command_id}>"
