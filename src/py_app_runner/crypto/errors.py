class CryptoError(Exception):
    """Anything wrong with key material, configuration, or a stored value's shape.

    Never caught and turned into a None by this package. A field that cannot be decrypted
    has to reach somebody: silently returning None puts an empty string in front of a user
    and leaves the real value sitting unreadable in the database, which is discovered
    months later when somebody asks why a column is full of blanks.
    """
