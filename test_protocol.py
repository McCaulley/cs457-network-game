"""Framing tests: coalescing, fragmentation and size limits."""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from protocol import (MAX_MSG_BYTES, FrameTooLargeError,  # noqa: E402
                      LineBuffer, encode_message, make_message)


def frame(alias: str = "Wade") -> bytes:
    return encode_message(make_message("CONNECT", None, {"alias": alias}))


class EncodeTests(unittest.TestCase):
    def test_single_trailing_newline(self):
        data = frame()
        self.assertTrue(data.endswith(b"\n"))
        self.assertEqual(data.count(b"\n"), 1)

    def test_newline_in_value_is_escaped(self):
        data = encode_message(make_message("ERROR", None,
                                           {"detail": "a\nb"}))
        self.assertEqual(data.count(b"\n"), 1)
        (line,) = LineBuffer().feed(data)
        self.assertEqual(json.loads(line)["payload"]["detail"], "a\nb")

    def test_oversized_message_rejected(self):
        with self.assertRaises(ValueError):
            encode_message(make_message("ERROR", None, {"detail": "x" * 5000}))


class LineBufferTests(unittest.TestCase):
    def test_one_message_one_chunk(self):
        self.assertEqual(len(LineBuffer().feed(frame())), 1)

    def test_coalesced(self):
        lines = LineBuffer().feed(frame("A") + frame("B"))
        self.assertEqual([json.loads(x)["payload"]["alias"] for x in lines],
                         ["A", "B"])

    def test_fragmented_inside_multibyte_character(self):
        data = encode_message(make_message("ERROR", None, {"detail": "é"}))
        data = data.replace(b"\\u00e9", "é".encode())   # raw 2-byte UTF-8
        cut = data.index("é".encode()) + 1              # split the char
        buf = LineBuffer()
        self.assertEqual(buf.feed(data[:5]), [])
        self.assertEqual(buf.feed(data[5:cut]), [])
        (line,) = buf.feed(data[cut:])
        self.assertEqual(json.loads(line)["payload"]["detail"], "é")

    def test_coalesced_and_fragmented(self):
        third = frame("C")
        buf = LineBuffer()
        self.assertEqual(len(buf.feed(frame("A") + frame("B") + third[:9])),
                         2)
        (line,) = buf.feed(third[9:])
        self.assertEqual(json.loads(line)["payload"]["alias"], "C")

    def test_one_byte_at_a_time(self):
        buf, got = LineBuffer(), []
        for byte in frame("A") + frame("B"):
            got += buf.feed(bytes([byte]))
        self.assertEqual(len(got), 2)

    def test_blank_lines_skipped(self):
        self.assertEqual(len(LineBuffer().feed(b"\n\r\n" + frame())), 1)

    def test_no_delimiter_too_large(self):
        with self.assertRaises(FrameTooLargeError):
            LineBuffer().feed(b"x" * (MAX_MSG_BYTES + 1))

    def test_oversized_complete_line(self):
        buf = LineBuffer()
        buf.feed(b"x" * MAX_MSG_BYTES)
        with self.assertRaises(FrameTooLargeError):
            buf.feed(b"x" * 10 + b"\n")


if __name__ == "__main__":
    unittest.main()
