import hashlib
import hmac
import json
import time
import unittest
from urllib.parse import urlencode

from bot.webauth import InitDataError, validate_init_data

BOT_TOKEN = "123456:TEST-TOKEN"


def sign(fields: dict[str, str], skip: set[str]) -> str:
    """Считает подпись по алгоритму Telegram: секрет выводится из токена бота."""
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    check_string = "\n".join(
        f"{key}={value}" for key, value in sorted(fields.items()) if key not in skip
    )
    return hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()


def make_init_data(
    *,
    auth_date: int | None = None,
    with_user: bool = True,
    user_json: str | None = None,
    with_signature: bool = False,
) -> str:
    fields: dict[str, str] = {"auth_date": str(auth_date or int(time.time()))}
    if with_user:
        fields["user"] = user_json or json.dumps({"id": 42, "first_name": "Ada"})
    if with_signature:
        fields["signature"] = "telegram-signature"
    fields["hash"] = sign(fields, {"hash", "signature"} if with_signature else {"hash"})
    return urlencode(fields)


class ValidateInitDataTests(unittest.TestCase):
    def test_valid_data_passes_and_returns_user(self) -> None:
        user = validate_init_data(make_init_data(), BOT_TOKEN)
        self.assertEqual(user["id"], 42)
        self.assertEqual(user["first_name"], "Ada")

    def test_valid_data_with_signature_field_passes(self) -> None:
        user = validate_init_data(make_init_data(with_signature=True), BOT_TOKEN)
        self.assertEqual(user["id"], 42)

    def test_tampered_field_is_rejected(self) -> None:
        data = make_init_data()
        tampered = data.replace(str(int(time.time())), str(int(time.time()) - 10))
        with self.assertRaises(InitDataError):
            validate_init_data(tampered, BOT_TOKEN)

    def test_wrong_token_is_rejected(self) -> None:
        with self.assertRaises(InitDataError):
            validate_init_data(make_init_data(), "999999:OTHER-TOKEN")

    def test_empty_data_is_rejected(self) -> None:
        with self.assertRaises(InitDataError):
            validate_init_data("", BOT_TOKEN)

    def test_missing_hash_is_rejected(self) -> None:
        data = make_init_data().split("&hash=")[0]
        with self.assertRaises(InitDataError):
            validate_init_data(data, BOT_TOKEN)

    def test_missing_auth_date_is_rejected(self) -> None:
        fields = {"user": json.dumps({"id": 42})}
        fields["hash"] = sign(fields, {"hash"})
        with self.assertRaises(InitDataError):
            validate_init_data(urlencode(fields), BOT_TOKEN)

    def test_missing_user_is_rejected(self) -> None:
        fields = {"auth_date": str(int(time.time()))}
        fields["hash"] = sign(fields, {"hash"})
        with self.assertRaises(InitDataError):
            validate_init_data(urlencode(fields), BOT_TOKEN)

    def test_stale_session_is_rejected(self) -> None:
        stale = int(time.time()) - 25 * 60 * 60
        with self.assertRaises(InitDataError):
            validate_init_data(make_init_data(auth_date=stale), BOT_TOKEN)

    def test_broken_user_json_is_rejected(self) -> None:
        fields = {"auth_date": str(int(time.time())), "user": "{not json"}
        fields["hash"] = sign(fields, {"hash"})
        with self.assertRaises(InitDataError):
            validate_init_data(urlencode(fields), BOT_TOKEN)

    def test_user_without_id_is_rejected(self) -> None:
        data = make_init_data(user_json=json.dumps({"first_name": "NoId"}))
        with self.assertRaises(InitDataError):
            validate_init_data(data, BOT_TOKEN)


if __name__ == "__main__":
    unittest.main()
