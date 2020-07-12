"""Useful utilities to be used by the ``Container`` class.

Some might be useful also for end users, like the wrappers to get streams,
like the ``LazyOpener``.
"""
import hashlib
import itertools
import os
import uuid
import zlib

from contextlib import contextmanager

from .exceptions import ModificationNotAllowed, ClosingNotAllowed


class LazyOpener:
    """A class to return a stream to a given file, that however is opened lazily.

    This means that upon instance creation no file is opened, while the file is opened
    when opening the stream.
    """

    def __init__(self, path, mode='rb'):
        """Lazily store file path and mode, but do not open now.

        File will be opened only when entering the context manager.
        """
        self._path = path
        self._mode = mode
        self._fhandle = None

    @property
    def path(self):
        """The file path."""
        return self._path

    @property
    def mode(self):
        """The file open mode."""
        return self._mode

    def tell(self):
        """Return the position in the underlying file object.

        :return: an integer with the position.
        :raises ValueError: if the file is not open.
        """
        if self._fhandle is None:
            raise ValueError('I/O operation on closed file.')
        return self._fhandle.tell()

    def __enter__(self):
        """Open the file when entering the with context manager.

        Note: you cannot open it twice with two with statements.
        """
        if self._fhandle is not None:
            raise IOError('File {} already open'.format(self.path))
        self._fhandle = open(self.path, mode=self.mode)
        return self._fhandle

    def __exit__(self, exc_type, value, traceback):
        """Close the file when exiting the with context manager."""
        if self._fhandle is not None:
            if not self._fhandle.closed:
                self._fhandle.close()
        self._fhandle = None


@contextmanager
def nullcontext(enter_result):
    """Return a context manager that returns enter_result from __enter__, but otherwise does nothing.

    This can be replaced by ``contextlib.nullcontext`` if we want to support only py>=3.7.
    """
    yield enter_result


class ObjectWriter:
    """A class to get direct write access for a new object."""

    def __init__(self, sandbox_folder, loose_folder, loose_prefix_len, hash_type, trust_existing=False):
        """Initialise an object to store a new loose object.

        :param sandbox_folder: the folder to store objects while still giving
          access to users to write them
        :param loose_folder: the folder to store the loose objects once finished
        :param loose_prefix_len: the length of the prefix to group loose objects
          in subfolders. E.g., 2 means store in folders aa/..., ab/...
          0 means store objects flat. Note that objects are always stored flat
          in the sandbox (there shouldn't be too many sandbox objects at the
          same time).
        :param trust_existing: if True, just continues if a file with the same hashkey is found.
          Otherwise, check the existing file content and overwrite if wrong.
        """
        self._sandbox_folder = sandbox_folder
        self._loose_folder = loose_folder
        self._hash_type = hash_type
        self._hashkey = None
        self._loose_prefix_len = loose_prefix_len
        self._stored = False
        self._obj_path = None
        self._filehandle = None
        self._trust_existing = trust_existing

    @property
    def hash_type(self):
        """Return the currently used hash type."""
        return self._hash_type

    def get_hashkey(self):
        """Return the object hash key. Return None if the stream wasn't opened yet."""
        return self._hashkey

    def __enter__(self):
        """Start creating a new object in a context manager.

        You will get access access to a file-like object.

        :note: Do not close the file, it will be closed automatically.
        """
        if self._filehandle is not None:
            raise IOError('You have already opened this ObjectWriter instance')
        if self._stored:
            raise ModificationNotAllowed("You have already stored this object '{}'".format(self.get_hashkey()))
        # Create a new uniquely-named file in the sandbox.
        # It seems faster than using a NamedTemporaryFile, see benchmarks.
        self._obj_path = os.path.join(self._sandbox_folder, uuid.uuid4().hex)
        self._filehandle = HashWriterWrapper(open(self._obj_path, 'wb'), hash_type=self.hash_type)
        return self._filehandle

    def __exit__(self, exc_type, value, traceback):  # pylint: disable=too-many-branches
        """
        Close the file object, and move it from the sandbox to the loose
        object folder, possibly using sharding if loose_prexix_len is not 0.
        """
        try:
            if exc_type is None:
                if self._filehandle.closed:
                    raise ClosingNotAllowed('You cannot close the file handle yourself!')
                # Flush out of the buffer, then fsync so it's written to disk; see e.g.
                # https://blog.gocept.com/2013/07/15/reliable-file-updates-with-python/
                self._filehandle.flush()
                os.fsync(self._filehandle.fileno())
                self._filehandle.close()
                self._hashkey = str(self._filehandle.hexdigest())
                self._filehandle = None

                if self._loose_prefix_len:
                    parent_folder = os.path.join(self._loose_folder, self._hashkey[:self._loose_prefix_len])
                    # Create parent folder the first time; done with try/except
                    # rather than with if/else to avoid problems at the beginning, for concurrent writing
                    try:
                        os.mkdir(parent_folder)
                    except FileExistsError:
                        # The folder already exists, great! No work to do
                        pass

                    dest_loose_object = os.path.join(
                        self._loose_folder, self._hashkey[:self._loose_prefix_len],
                        self._hashkey[self._loose_prefix_len:]
                    )
                else:  # prefix_len == 0
                    dest_loose_object = os.path.join(self._loose_folder, self._hashkey)

                dest_parent_folder = os.path.dirname(dest_loose_object)
                # At this point, if the destination exists, it means someone already put an object
                # with the same content/hash key
                if os.path.exists(dest_loose_object):
                    if self._trust_existing:
                        # I trust that the object is correct: I just return
                        # Note: if another process is deleting the file at the same time (right after),
                        # it will be deleted. But this is OK - the deletion is in an independent process,
                        # an if it happens only microseconds or seconds after the write, the effect is the same:
                        # the object is deleted and the caller of this function will not find it anymore.
                        return
                    # I do not trust the object is correct.
                    # This might still not be perfect: while I check, another
                    # process might in the meantime rewrite it again, and this might
                    # be wrong. But this is very difficult to catch, and in general
                    # the situation in which a process writes a corrupt node is really an error
                    existing_checksum = _compute_hash_for_filename(filename=dest_loose_object, hash_type=self.hash_type)
                    if existing_checksum == self._hashkey:
                        # The existing object has the correct hash, I just return.
                        return
                    # If existing_checksum is None, the file has been removed in the meantime.
                    # It could be a delete of the object: then, for consistency with the logic above,
                    # I decide that the two operations that happened almost at the same time could
                    # have happened in swapped order, and I don't write it back.
                    # Or it's a packing operation (and then it's OK to not put the file back).
                    # I therefore just return
                    if existing_checksum is None:
                        return

                # If I am here, there are two options:
                # 1. The object did not exist
                # 2. The file is there but its content is wrong. In this case I want to overwrite the object.

                # I therefore call a 'replace' call, that should be an atomic operation
                # Notes (on os.replace vs os.rename):
                # - os.rename() on Linux is atomic (see e.g. also
                #   https://alexwlchan.net/2019/03/atomic-cross-filesystem-moves-in-python/
                #   but needs to be on the same filesystem (this should always be the case for us)
                # - Remembe that instead shutil.move is not guaranteed to be atomic!
                # - os.rename performs a silent overwrite if the file exists on Posix, so would be enough
                #   on Linux/Mac; instead, it raises OSError on Windows if the file exists.
                # - we use instead os.replace that, on Windows, calls a rename with the correct flags
                #   to overwrite an existing destination.
                # NOTE: for now I don't catch any exception on Windows. I am not sure of which race conditions
                # might happen yet, e.g. if someone has opened the file.
                # But either this was corrupt and open, and then it shouldn't really be open by someone...
                # Or it had been deleted in the meantime, so I should be creating it (someone might be reopening
                # it at the same time, this is probably a rare but possible race condition)
                os.replace(self._obj_path, dest_loose_object)

                # Flush also the parent directory, see e.g.
                # https://blog.gocept.com/2013/07/15/reliable-file-updates-with-python/
                if os.name == 'posix':
                    dirfd = os.open(os.path.dirname(dest_parent_folder), os.O_DIRECTORY)
                    os.fsync(dirfd)
                    os.close(dirfd)
                self._stored = True
        finally:
            if self._filehandle is not None and not self._filehandle.closed:
                self._filehandle.close()
            if os.path.exists(self._obj_path):
                os.remove(self._obj_path)


class PackedObjectReader:
    """A class to read from a pack file.

    This ensures that the .read() method works and does not go beyond the
    length of the given object."""

    @property
    def seekable(self):
        """Return whether object supports random access."""
        return False

    def seek(self, target, whence=0):  # pylint: disable=no-self-use
        """Change stream position."""
        raise OSError('Object not seekable')

    def tell(self):  # pylint: disable=no-self-use
        """Return current stream position."""
        raise OSError('Object not seekable')

    def __init__(self, fhandle, offset, length):
        """
        Initialises the reader to a pack file.

        :param fhandle: an open handle to the pack file, must be opened in read and binary mode.
        :param offset: the integer offset where in the fhandle where the object starts.
        :param length: the integer length of the byte stream.
          The read() method will ensure that you never go beyond the given length.
        """
        assert 'b' in fhandle.mode
        assert 'r' in fhandle.mode

        self._fhandle = fhandle
        self._offset = offset
        self._length = length

        # Move to the offset position, and keep track of the internal position
        self._fhandle.seek(self._offset)
        self._update_pos()

    def _update_pos(self):
        """Update the internal position variable with the correct value.

        This function must be called (internally) after any operation that
        moves into the file.
        """
        self._pos = self._fhandle.tell() - self._offset
        assert self._pos <= self._length, RuntimeError(
            "PackedObjectReader didn't manage to prevent to go beyond the length!"
        )

        assert self._pos >= 0, RuntimeError(
            "PackedObjectReader didn't manage to prevent to go beyond the length (in the negative direction)!"
        )

    def read(self, size=-1):
        """
        Read and return up to n bytes.

        If the argument is omitted, None, or negative, reads and
        returns all data until EOF (that corresponds to the length specified
        in the __init__ method).

        Returns an empty bytes object on EOF.
        """
        # Check how many bytes are left on this portion of the pack
        # (avoid to go beyond)
        remaining_bytes = self._length - self._pos

        if size is None or size < 0:
            stream = self._fhandle.read(remaining_bytes)
            self._update_pos()
            return stream

        # Get the requested bytes, but at most the remaining_bytes
        bytes_to_fetch = min(remaining_bytes, size)
        stream = self._fhandle.read(bytes_to_fetch)
        self._update_pos()
        return stream


class StreamDecompresser:
    """A class that gets a stream of compressed zlib bytes, and returns the corresponding
    uncompressed bytes when being read via the .read() method.
    """

    _CHUNKSIZE = 524288

    def __init__(self, compressed_stream):
        """Create the class from a given compressed bytestream.

        :param compressed_stream: an open bytes stream that supports the .read() method,
          returning a valid compressed stream.
        """
        self._compressed_stream = compressed_stream
        self._decompressor = zlib.decompressobj()
        self._internal_buffer = b''

    def read(self, size=-1):
        """
        Read and return up to n bytes.

        If the argument is omitted, None, or negative, reads and
        returns all data until EOF (that corresponds to the length specified
        in the __init__ method).

        Returns an empty bytes object on EOF.
        """
        if size is None or size < 0:
            # Read all the rest: we call ourselves but with a length,
            # and return the joined result
            data = []
            while True:
                next_chunk = self.read(self._CHUNKSIZE)
                if not next_chunk:
                    # Empty returned value: EOF
                    break
                data.append(next_chunk)
            # Making a list and joining does many less mallocs, so should be faster
            return b''.join(data)

        if size == 0:
            return b''

        while len(self._internal_buffer) < size:
            old_unconsumed = self._decompressor.unconsumed_tail
            next_chunk = self._compressed_stream.read(max(0, self._CHUNKSIZE - len(old_unconsumed)))

            # In the previous step, I might have some leftover data
            # since I am using the max_size parameter of .decompress()
            compressed_chunk = old_unconsumed + next_chunk
            # The second parameter is max_size. We know that in any case we do
            # not need more than `size` bytes. Leftovers will be left in
            # .unconsumed_tail and reused a the next loop
            try:
                decompressed_chunk = self._decompressor.decompress(compressed_chunk, size)
            except zlib.error as exc:
                raise ValueError('Error while uncompressing data: {}'.format(exc))
            self._internal_buffer += decompressed_chunk

            if not next_chunk and not self._decompressor.unconsumed_tail:
                # Nothing to do: no data read, and the unconsumed tail is over.
                if self._decompressor.eof:
                    # Compressed file is over. We break
                    break
                raise ValueError(
                    "There is no data in the reading buffer, but we didn't reach the end of "
                    'the compressed stream: there must be a problem in the incoming buffer'
                )

        # Note that we could be here also with len(self._internal_buffer) < size,
        # if we used 'break' because the internal buffer reached EOF.
        to_return, self._internal_buffer = self._internal_buffer[:size], self._internal_buffer[size:]

        return to_return


class ZeroStream:
    """A class to return an (unseekable) stream returning only zeros, with length length."""

    def __init__(self, length):
        """
        Initialises the object and specifies the expected length.

        :param length: the integer length of the byte stream.
          The read() method will return this number of bytes.
        """
        self._length = length
        self._pos = 0

    def read(self, size=-1):
        """
        Read and return up to n bytes (composed only of zeros).

        If the argument is omitted, None, or negative, reads and
        returns all data until EOF (that corresponds to the length specified
        in the __init__ method).

        Returns an empty bytes object on EOF.
        """
        # Check how many bytes are left on this portion of the pack
        # (avoid to go beyond)
        remaining_bytes = self._length - self._pos

        if size is None or size < 0:
            stream = b'\x00' * remaining_bytes
            self._pos += remaining_bytes
            return stream

        # Get the requested bytes, but at most the remaining_bytes
        bytes_to_fetch = min(remaining_bytes, size)
        stream = b'\x00' * bytes_to_fetch
        self._pos += bytes_to_fetch
        return stream


def is_known_hash(hash_type):
    """Return True if the hash_type is known, False otherwise."""
    try:
        _get_hash(hash_type)
        return True
    except ValueError:
        return False


def _get_hash(hash_type):
    """Return a hash class with an update method and a hexdigest method."""
    known_hashes = {
        'sha256': hashlib.sha256,
    }

    try:
        return known_hashes[hash_type]
    except KeyError:
        raise ValueError("Unknown or unsupported hash type '{}'".format(hash_type))


def _compute_hash_for_filename(filename, hash_type):
    """Return the hash for the given file.

    Will read the file in chunks.

    :param filename: a filename to a file to check.
    :param hash_type: a valid string as recognised by the _get_hash function
    :return: the hash hexdigest (the hash key), or `None` if the file does not exist
    """
    _chunksize = 524288

    hasher = _get_hash(hash_type)()
    try:
        with open(filename, 'rb') as fhandle:
            while True:
                chunk = fhandle.read(_chunksize)
                hasher.update(chunk)

                if not chunk:
                    # Empty returned value: EOF
                    break
    except FileNotFoundError:
        return None

    return hasher.hexdigest()


class HashWriterWrapper:
    """A class that gets a stream open in write mode and wraps it in a new class that computes a hash while writing.
    """

    def __init__(self, write_stream, hash_type):
        """Create the class from a given compressed bytestream.

        :param write_stream: an open bytes stream that supports the .write() method.
           Must be a ``bytes`` stream (e.g., a file opened with ``b`` in the mode).
           Writes to this class will be forwarded to the write_stream, but will first
           be also passed to a hashlib implementation to compute the hash.
        """
        self._write_stream = write_stream
        assert 'b' in self._write_stream.mode
        self._hash_type = hash_type

        self._hash = _get_hash(self._hash_type)()
        self._position = self._write_stream.tell()

    @property
    def hash_type(self):
        """Return the currently used hash type."""
        return self._hash_type

    def flush(self):
        """Flush the internal I/O buffer."""
        self._write_stream.flush()

    def write(self, data):
        """
        Write binary data to the underlying write_stream object, and compute a hash at the same time.
        """
        # This assert is important to avoid that there is an exception while writing.
        # In this case, the hash function does not get the data, but part of it might have already been
        # written to disk (e.g. a first chunk of bytes, and then the disk became full).
        # We want to make sure we are computing the correct hash.
        assert self._position == self._write_stream.tell(), (
            'Error in the position ({} vs {}), possibly an error occurred in a previous `write` call. '
            'This HashWriterMapper object is invalid and should not be used anymore'.format(
                self._position, self._write_stream.tell()
            )
        )

        self._write_stream.write(data)
        # Update the length after a successful write. Otherwise we will stay at the previous position.
        # If nothing was written, then the next call will succeed (the position remained the same).
        # If the call wrote something, the next call will correctly assert (there would be a mismatch
        # between the data on the write_stream and the one that was hashed).
        self._position += len(data)

        # Update the hash information
        self._hash.update(data)

    def hexdigest(self):
        """Return the hexdigest of the hash computed until now."""
        return self._hash.hexdigest()

    @property
    def closed(self):
        """Return True if the underlying file is closed."""
        return self._write_stream.closed

    def close(self):
        """Close the underlying file."""
        self._write_stream.close()

    @property
    def mode(self):
        """Return a string with the mode the file was open with."""
        return self._write_stream.mode

    def fileno(self):
        """Return the integer file descriptor of the underlying file object."""
        return self._write_stream.fileno()


def chunk_iterator(iterator, size):
    """Given an iterator, split it in chunks of size `size` (except the last one, that can be shorter).

    Examples:

    - `list(chunk_iterator(range(10), 3))` returns `[(0, 1, 2), (3, 4, 5), (6, 7, 8), (9,)]`
    - `list(chunk_iterator(range(9), 3))` returns `[(0, 1, 2), (3, 4, 5), (6, 7, 8)]`

    :param iterator: an iterator to divide in chunks
    :param size: the size of each chunk
    """
    iterator = iter(iterator)
    return iter(lambda: tuple(itertools.islice(iterator, size)), ())
