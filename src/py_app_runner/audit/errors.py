class AuditError(Exception):
    """A refusal or a failed write in the audit trail.

    Raised rather than logged when `strict` is on, which is the default: an audit trail
    that quietly stops recording is worse than one that is obviously broken, because the
    gap is only discovered when somebody goes looking for the row that should have been
    there.
    """
