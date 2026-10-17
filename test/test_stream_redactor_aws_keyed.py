"""Streamed AWS key-value pairs whose value is longer than the stream floor.

The batch AWS key-value branches redact a value only while its key label is in
the same ``redact()`` call. The stream redactor holds a label and its value
together up to ``_STREAM_HOLDBACK_JWT_MAX`` and fails closed past it.
"""

from __future__ import annotations

import random
import re

import pytest

from kiro_crew.security import (
    _CRED_CLASS,
    _STREAM_HOLDBACK_JWT_MAX,
    REDACTED_CREDENTIAL_TAG,
    StreamRedactor,
    redact,
    stream_aws_anchor,
)


def _natural_cut(buf: str) -> int:
    cut = len(buf)
    while cut > 0 and buf[cut - 1] in _CRED_CLASS:
        cut -= 1
    return cut


def _stream(text: str, chunk: int) -> str:
    redactor = StreamRedactor()
    out = "".join(redactor.feed(text[i : i + chunk]) for i in range(0, len(text), chunk))
    return out + redactor.flush()


@pytest.mark.parametrize(
    "template",
    [
        "aws_secret_access_key={v}\nnext",
        'aws_session_token = "{v}"\nnext',
        '{{"SessionToken": "{v}"}}\nnext',
        "SecretAccessKey: {v}\nnext",
        "AccessKeyId={v} next",
    ],
)
@pytest.mark.parametrize("length", [600, 3000, 5000])
@pytest.mark.parametrize("chunk", [1, 7, 64, 600])
def test_aws_keyed_value_past_the_floor_never_streams_raw(
    template: str, length: int, chunk: int
) -> None:
    """The value has no 40-character base64 run, so only the key-value branch can
    redact it. Up to the ceiling it is redacted whole; past it the stream fails
    closed. Either way no piece of the value reaches the output."""
    pieces = [f"v{i:09d}k-" for i in range(length // 12)]
    value = "".join(pieces)
    out = _stream("hello " + template.format(v=value), chunk)
    assert [p for p in pieces if p in out] == []
    assert out.startswith("hello ")
    assert out.rstrip().endswith("next")


@pytest.mark.parametrize("chunk", [1, 7, 64])
def test_aws_keyed_value_under_the_ceiling_streams_as_the_batch_pass_writes_it(
    chunk: int,
) -> None:
    value = "".join(f"v{i:09d}k-" for i in range(100))
    text = 'cfg {"SessionToken": "' + value + '", "region": "x"} end'
    assert _stream(text, chunk) == redact(text)


def test_oversized_aws_keyed_continuation_is_discarded() -> None:
    redactor = StreamRedactor()
    head = "aws_secret_access_key=" + "S" * (_STREAM_HOLDBACK_JWT_MAX + 500)
    continuation = "tail_u7-Qp9_" * 24
    joined = redactor.feed(head) + redactor.feed(continuation + " done") + redactor.flush()
    assert all(
        continuation[start : start + 32] not in joined for start in range(len(continuation) - 31)
    )
    assert joined.count(REDACTED_CREDENTIAL_TAG) == 1
    assert joined.endswith(" done")


def test_aws_keyed_discard_stops_at_the_value_terminator() -> None:
    redactor = StreamRedactor()
    head = "SecretAccessKey: " + "S" * (_STREAM_HOLDBACK_JWT_MAX + 500)
    assert redactor.feed(head).endswith(REDACTED_CREDENTIAL_TAG)
    assert redactor.discarding
    assert redactor.feed("SecretTail, done") + redactor.flush() == ", done"


@pytest.mark.parametrize(
    "label",
    ["SessionToken:", "SessionToken: ", 'SessionToken"', "SessionToken", "aws_session_token ="],
)
def test_a_label_padded_past_the_ceiling_still_hides_its_quoted_value(label: str) -> None:
    """The discard walks the rest of the label grammar before value bytes: the
    opening quote that follows the padding cannot end it."""
    redactor = StreamRedactor()
    value = "tail_u7-Qp9_" * 90
    out = redactor.feed(label + " " * (_STREAM_HOLDBACK_JWT_MAX + 100))
    if not label.rstrip().endswith((":", "=")):
        out += redactor.feed(": ")
    out += redactor.feed('"' + value + '" done') + redactor.flush()
    assert value[:24] not in out
    assert out.endswith(" done")


def test_a_nested_token_parameter_does_not_end_the_outer_aws_value() -> None:
    redactor = StreamRedactor()
    head = "SessionToken=" + "v0-" * 10 + "?token=" + "v1-" * 1400
    tail = "tail_u7-Qp9_" * 90
    out = redactor.feed(head) + redactor.feed("&" + tail + " done") + redactor.flush()
    assert tail[:24] not in out
    assert out.endswith(" done")


def test_a_closed_value_past_the_ceiling_keeps_its_suffix() -> None:
    value = "".join(f"v{i:09d}k-" for i in range(400))
    out = _stream('SessionToken: "' + value + '"abc, done', 7)
    assert value[:24] not in out
    assert out.endswith(", done")


def test_a_padded_key_with_no_separator_is_released_unchanged() -> None:
    text = (
        "SessionToken"
        + " " * (_STREAM_HOLDBACK_JWT_MAX + 100)
        + "is a field name, not a credential"
    )
    redactor = StreamRedactor()
    out = redactor.feed(text[: len(text) - 33]) + redactor.feed(text[-33:]) + redactor.flush()
    assert out == text


def test_a_repeated_label_run_takes_few_anchored_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The anchor is found without rescanning the value once per label."""
    calls: list[int] = []
    real = stream_aws_anchor.anchor_re(_CRED_CLASS)

    class _Counting:
        def match(self, buf: str, pos: int = 0) -> object:
            calls.append(pos)
            return real.match(buf, pos)

    monkeypatch.setattr(stream_aws_anchor, "anchor_re", lambda _cred: _Counting())
    buf = "SessionToken=x" * 8500 + " "
    stream_aws_anchor.find(buf, _CRED_CLASS, _natural_cut(buf))
    assert len(calls) <= 4


@pytest.mark.parametrize("seed", range(40))
def test_the_linear_anchor_finds_what_the_regex_search_finds(seed: int) -> None:
    rng = random.Random(seed)
    parts = ["SessionToken", "aws_session_token", "AccessKeyId", ":", "=", " ", '"', "'"]
    parts += [",", "}", "x", "ab-", "\n", "?token=", "&"]
    for _ in range(200):
        buf = "".join(rng.choice(parts) for _ in range(rng.randint(0, 14)))
        want = stream_aws_anchor.anchor_re(_CRED_CLASS).search(buf)
        got = stream_aws_anchor.find(buf, _CRED_CLASS, _natural_cut(buf))
        assert (want and want.span()) == (got and got.span()), repr(buf)


def test_a_key_mentioned_in_prose_is_released() -> None:
    text = "set aws_session_token before you run it, then retry"
    assert _stream(text, 1) == text


@pytest.mark.parametrize(
    "label",
    ["aws_session_token=", "Authorization: Bearer "],
)
@pytest.mark.parametrize("tail_key", ["AccessKeyId", "SecretAccessKey", "aws_session_token"])
@pytest.mark.parametrize("chunk", [None, 1, 7])
def test_a_key_name_ending_another_credentials_value_does_not_split_it(
    label: str, tail_key: str, chunk: int | None
) -> None:
    """A credential value may end in text that spells an AWS key name. The stream
    must keep that whole value with its own label, so the output equals the batch
    pass and no tail of the value streams raw."""
    text = label + "v0-" * 10 + tail_key + " done"
    if chunk is None:
        redactor = StreamRedactor()
        out = redactor.feed(label + "v0-" * 10 + tail_key + " ") + redactor.feed("done")
        out += redactor.flush()
    else:
        out = _stream(text, chunk)
    assert out == redact(text)
    assert tail_key not in out


@pytest.mark.parametrize("tail_key", ["AccessKeyId", "SessionToken", "SecretAccessKey"])
@pytest.mark.parametrize("chunk", [None, 1, 7])
def test_a_bare_secret_ending_in_a_key_name_is_not_split(tail_key: str, chunk: int | None) -> None:
    """A 40-character run whose last bytes spell a key name is judged only by the
    batch bare-secret pass, which needs the whole run. The stream must not cut the
    run at the key name, so it hides exactly what the batch pass hides."""
    secret = "Zx9Qw3Er7Ty1Ui5Op2As8Df4Gh6Jk0Lm"[: 40 - len(tail_key)] + tail_key
    text = "x " + secret + " done"
    if chunk is None:
        redactor = StreamRedactor()
        out = redactor.feed("x " + secret + " ") + redactor.feed("done") + redactor.flush()
    else:
        out = _stream(text, chunk)
    assert out == redact(text)


def test_a_key_name_ending_a_long_value_leaves_no_label_open() -> None:
    """A key name at the end of a credential value past the ceiling is part of that
    value. Releasing it must not remember an open label, or the next word after a
    colon would be dropped as if it were a value."""
    head = "SessionToken=" + "a-" * 2300 + "AccessKeyId "
    redactor = StreamRedactor()
    out = redactor.feed(head) + redactor.feed(": visible text") + redactor.flush()
    assert out == redact(head + ": visible text")
    assert out.endswith(": visible text")


def test_every_aws_label_rule_has_the_shape_the_stream_anchor_reads() -> None:
    """The stream anchor reads its key names off the batch AWS label rules. A rule
    of another shape, or a key name that is not a plain literal, would make the
    anchor miss that label, so the mismatch fails here instead of at import."""
    from kiro_crew.security import redaction

    for label, _rule in redaction._AWS_LABEL_RULES:
        assert label.pattern.endswith(stream_aws_anchor._LABEL_TAIL_RE), label.pattern
    assert stream_aws_anchor._KEY_LITERALS
    for name in stream_aws_anchor._KEY_LITERALS:
        assert re.fullmatch(r"\w+", name), name


@pytest.mark.parametrize("query", ["?data=!", "?data=", "?q=x;"])
def test_a_url_carrying_an_encoded_key_before_a_key_name_is_not_split(query: str) -> None:
    """A key name can end a URL whose query carries an encoded access key. When
    the URL arrives in one piece, the AWS cut must not split it: the whole URL
    reaches one batch call, so the stream hides what the batch hides."""
    encoded = "".join(f"%{ord(c):02X}" for c in "AKIAIOSFODNN7EXAMPLE")
    head = "https://example.test/" + query + encoded + "AccessKeyId "
    redactor = StreamRedactor()
    out = redactor.feed(head) + redactor.feed("done") + redactor.flush()
    assert out == redact(head + "done")
    assert encoded not in out


def test_an_aws_label_inside_a_long_bearer_value_drops_nothing_after_it() -> None:
    """A key name with a separator inside a Bearer value is part of that value. Past
    the ceiling the drop must end where the Bearer value ends, as the batch pass
    does, not run on through the AWS value class into the text after it."""
    head = "Authorization: Bearer " + "a-" * 2300 + "AccessKeyId="
    redactor = StreamRedactor()
    out = redactor.feed(head) + redactor.feed(":BENIGN_SUFFIX done") + redactor.flush()
    assert out == redact(head + ":BENIGN_SUFFIX done")
    assert out.endswith(":BENIGN_SUFFIX done")


@pytest.mark.parametrize("quote", ['"', "'"])
def test_a_quote_after_a_padded_key_does_not_open_its_label(quote: str) -> None:
    """The batch grammar allows a key quote only directly after the key. A quote
    that arrives after the key's padding was released opens no label, so the text
    after the next colon streams unchanged."""
    head = "SessionToken" + " " * 4200
    tail = quote + " : visible text"
    redactor = StreamRedactor()
    out = redactor.feed(head) + redactor.feed(tail) + redactor.flush()
    assert out == redact(head + tail)


def test_a_released_quoted_key_still_hides_the_value_after_its_closing_quote() -> None:
    """A JSON key released past the ceiling before its closing quote arrived still
    owns that quote. The value after `": "` must not stream raw."""
    token = "".join(chr(ord("a") + (i * 7) % 26) for i in range(720))
    head = '{"padding":"' + "a-" * 2300 + '","SessionToken'
    redactor = StreamRedactor()
    out = redactor.feed(head) + redactor.feed('": "' + token + '"}') + redactor.flush()
    assert token[:40] not in out


def test_minified_json_past_the_ceiling_keeps_the_fields_before_the_key() -> None:
    """With no whitespace in the buffer, the AWS cut must not walk back past the
    ceiling window, or the fail-closed drop takes the benign fields before the key."""
    policy = ",".join(f"p{i:05d}" for i in range(500))
    text = '{"Policy":"' + policy + '","SessionToken":"' + "Z" * 5000 + '","x":1} done'
    out = _stream(text, 50)
    assert "p00499" in out
    assert "ZZZZZZZZ" not in out
