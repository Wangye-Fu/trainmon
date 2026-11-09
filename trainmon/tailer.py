import os


class Tailer(object):
    def __init__(self, path, initial_bytes=262144, chunk_bytes=262144,
                 max_lines=5000):
        self.path = path
        self.initial_bytes = int(initial_bytes)
        self.chunk_bytes = max(1024, int(chunk_bytes))
        self.max_lines = int(max_lines)
        self.offset = 0
        self.inode = None
        self.partial = b""
        self.initialized = False
        self.dropped = 0

    def _stat(self):
        try:
            st = os.stat(self.path)
        except (IOError, OSError):
            return None
        return st

    def poll(self):
        if not self.path:
            return []
        st = self._stat()
        if st is None:
            self.initialized = False
            self.offset = 0
            self.inode = None
            self.partial = b""
            return []
        inode = (getattr(st, "st_dev", 0), getattr(st, "st_ino", 0))
        size = int(st.st_size)
        drop_first = False
        if not self.initialized:
            self.inode = inode
            self.offset = max(0, size - self.initial_bytes)
            self.partial = b""
            drop_first = self.offset > 0
            self.initialized = True
        elif self.inode != inode or size < self.offset:
            # Log rotated or truncated: restart from the beginning.
            self.inode = inode
            self.offset = 0
            self.partial = b""
        if size <= self.offset:
            return []
        if size - self.offset > self.chunk_bytes:
            self.offset = size - self.chunk_bytes
            self.partial = b""
            drop_first = True
            self.dropped += 1
        read_len = min(size - self.offset, self.chunk_bytes)
        try:
            with open(self.path, "rb") as handle:
                handle.seek(self.offset)
                data = handle.read(read_len)
        except (IOError, OSError):
            return []
        self.offset += len(data)
        if not data:
            return []
        normalized = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        buffer = self.partial + normalized
        parts = buffer.split(b"\n")
        self.partial = parts.pop()
        if len(self.partial) > 1024 * 1024:
            self.partial = self.partial[-1024 * 1024:]
        if drop_first and parts:
            parts = parts[1:]
        lines = []
        for raw in parts:
            text = raw.decode("utf-8", "replace").strip("\x00")
            if text.strip():
                lines.append(text)
        if len(lines) > self.max_lines:
            self.dropped += len(lines) - self.max_lines
            lines = lines[-self.max_lines:]
        return lines
