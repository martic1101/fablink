import pytest

from app.secs.codec import Fmt, Item, SecsDecodeError, decode, encode


@pytest.mark.parametrize(
    "item",
    [
        Item.A("HELLO"),
        Item.A(""),
        Item.B(b"\x00\x01\xff"),
        Item.BOOL(True, False),
        Item.U1(0, 255),
        Item.U4(0, 4_294_967_295),
        Item.I4(-2_147_483_648, 7),
        Item(Fmt.I1, [-128, 127]),
        Item(Fmt.U8, [2**64 - 1]),
        Item.F8(3.141592653589793),
        Item.L(),
        Item.L(Item.U4(1), Item.L(Item.A("nested"), Item.F8(1.5))),
    ],
)
def test_roundtrip(item):
    assert decode(encode(item)) == item


def test_known_encoding_matches_semi_e5():
    # <L[2] <A "AB"> <U4 1>> — format byte for L is 0x01, A is 0x41, U4 is 0xB1
    assert encode(Item.L(Item.A("AB"), Item.U4(1))) == bytes(
        [0x01, 0x02, 0x41, 0x02, ord("A"), ord("B"), 0xB1, 0x04, 0, 0, 0, 1]
    )


@pytest.mark.parametrize("size,n_len", [(255, 1), (256, 2), (65535, 2), (65536, 3)])
def test_length_byte_count_grows_with_payload(size, n_len):
    raw = encode(Item.B(b"x" * size))
    assert raw[0] & 0b11 == n_len
    assert decode(raw).value == b"x" * size


def test_f4_loses_precision_but_roundtrips():
    out = decode(encode(Item.F4(0.1)))
    assert out.value[0] == pytest.approx(0.1, rel=1e-6)


@pytest.mark.parametrize(
    "raw,msg",
    [
        (b"", "end of data"),
        (bytes([0x41, 0x05, ord("a")]), "truncated"),
        (bytes([0x40]), "zero length bytes"),
        (bytes([0xFD, 0x00]), "unknown format"),
        (bytes([0xB1, 0x03, 0, 0, 1]), "not a multiple"),
        (bytes([0x41, 0x00, 0x00]), "trailing"),
        (bytes([0x01, 0x02, 0x41, 0x00]), "end of data"),  # list claims 2 children, has 1
    ],
)
def test_malformed_input_raises(raw, msg):
    with pytest.raises(SecsDecodeError, match=msg):
        decode(raw)


def test_sml_rendering():
    sml = Item.L(Item.A("START"), Item.L(Item.U4(3))).to_sml()
    assert sml == '<L[2]\n  <A "START">\n  <L[1]\n    <U4 3>\n  >\n>'


def test_accessors():
    item = Item.L(Item.U4(7), Item.A("x"))
    assert item[0].scalar == 7
    assert item.to_python() == [7, "x"]
    with pytest.raises(TypeError):
        item[0][0]
