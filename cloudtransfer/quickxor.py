"""QuickXorHash - the only content hash OneDrive for Business reports.

Microsoft Graph gives different hashes depending on the account type:

* OneDrive Personal   -> ``sha1Hash``, ``sha256Hash``, ``quickXorHash``
* OneDrive for Business / SharePoint -> ``quickXorHash`` only

Google Drive, meanwhile, reports ``md5Checksum`` (and ``sha1Checksum`` /
``sha256Checksum`` on newer files). The two sets only overlap when the
destination is a personal account, so to verify a business transfer at all we
have to compute QuickXorHash ourselves while the bytes stream past.

This is a faithful port of Microsoft's published reference implementation. The
algorithm XORs input byte ``i`` into bit offset ``(i * 11) mod 160`` of a
160-bit accumulator, then XORs the total length into the trailing 8 bytes.
Because the bit offset depends only on ``i mod 160``, the state can be carried
across arbitrarily sized blocks, which is what makes it usable on a stream.
"""

from __future__ import annotations

import base64

_U64 = 0xFFFF_FFFF_FFFF_FFFF


class QuickXorHash:
    """Incremental QuickXorHash. Feed bytes with :meth:`update`."""

    WIDTH_IN_BITS = 160
    SHIFT = 11
    BITS_IN_LAST_CELL = 32
    _CELLS = 3  # ceil(160 / 64)

    __slots__ = ("_data", "_length_so_far", "_shift_so_far")

    def __init__(self) -> None:
        self._data = [0] * self._CELLS
        self._length_so_far = 0
        self._shift_so_far = 0

    def update(self, block: bytes | bytearray | memoryview) -> None:
        if not block:
            return
        block = bytes(block)
        cb_size = len(block)

        current_shift = self._shift_so_far
        vector_array_index = current_shift // 64
        vector_offset = current_shift % 64
        iterations = min(cb_size, self.WIDTH_IN_BITS)

        data = self._data
        for i in range(iterations):
            is_last_cell = vector_array_index == self._CELLS - 1
            bits_in_vector_cell = self.BITS_IN_LAST_CELL if is_last_cell else 64

            if vector_offset <= bits_in_vector_cell - 8:
                # The whole byte fits inside this cell.
                for j in range(i, cb_size, self.WIDTH_IN_BITS):
                    data[vector_array_index] ^= block[j] << vector_offset
            else:
                # The byte straddles two cells (wrapping to cell 0 at the end).
                index1 = vector_array_index
                index2 = 0 if is_last_cell else vector_array_index + 1
                low = bits_in_vector_cell - vector_offset

                xored_byte = 0
                for j in range(i, cb_size, self.WIDTH_IN_BITS):
                    xored_byte ^= block[j]
                data[index1] ^= xored_byte << vector_offset
                data[index2] ^= xored_byte >> low

            vector_offset += self.SHIFT
            while vector_offset >= bits_in_vector_cell:
                vector_array_index = 0 if is_last_cell else vector_array_index + 1
                vector_offset -= bits_in_vector_cell

        # The reference implementation works on fixed-width ulongs, where bits
        # shifted past the top are simply dropped. Python integers are
        # unbounded, so trim them here. XOR is bitwise, so masking once at the
        # end is equivalent to masking every operation.
        for k in range(self._CELLS):
            data[k] &= _U64

        self._shift_so_far = (
            self._shift_so_far + self.SHIFT * (cb_size % self.WIDTH_IN_BITS)
        ) % self.WIDTH_IN_BITS
        self._length_so_far += cb_size

    def digest(self) -> bytes:
        """Return the 20-byte hash."""
        size = self.WIDTH_IN_BITS // 8  # 20
        rgb = bytearray(size)

        full_cells = self._CELLS - 1
        for i in range(full_cells):
            rgb[i * 8 : (i + 1) * 8] = (self._data[i] & _U64).to_bytes(8, "little")

        tail = size - full_cells * 8  # 4 bytes of the final cell
        rgb[full_cells * 8 :] = (self._data[-1] & _U64).to_bytes(8, "little")[:tail]

        length_bytes = (self._length_so_far & _U64).to_bytes(8, "little")
        offset = size - len(length_bytes)  # 12
        for i, byte in enumerate(length_bytes):
            rgb[offset + i] ^= byte

        return bytes(rgb)

    def hexdigest(self) -> str:
        return self.digest().hex()

    def b64digest(self) -> str:
        """Base64 form, directly comparable to Graph's ``quickXorHash``."""
        return base64.b64encode(self.digest()).decode("ascii")


def quickxorhash(data: bytes) -> str:
    """One-shot convenience wrapper returning the base64 digest."""
    h = QuickXorHash()
    h.update(data)
    return h.b64digest()
