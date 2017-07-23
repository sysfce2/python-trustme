import datetime
import ssl
from tempfile import NamedTemporaryFile
from contextlib import contextmanager
import os

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import (
    PrivateFormat, NoEncryption
)
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives.serialization import Encoding

from ._version import __version__

__all__ = ["CA"]

# On my laptop, making a CA + server certificate using 1024 bit keys takes ~40
# ms, and using 4096 bit keys takes ~2 seconds. We want tests to run in 40 ms,
# not 2 seconds.
_KEY_SIZE = 1024

def _smells_like_pyopenssl(ctx):
    return getattr(ctx, "__module__", "").startswith("OpenSSL")

def _common_name(name):
    name += " (generated by faketlscerts v{})".format(__version__)
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])

def _cert_builder_common(subject, issuer, public_key):
    today = datetime.datetime.today()
    yesterday = today - datetime.timedelta(1, 0, 0)
    forever = today.replace(year=today.year + 1000)
    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        # This is inclusive so today should work too, but let's pad it a
        # bit.
        .not_valid_before(yesterday)
        .not_valid_after(forever)
        .serial_number(x509.random_serial_number())
        .public_key(public_key)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(public_key),
            critical=False,
        )
    )


class Blob(object):
    """A convenience wrapper for a blob of bytes.

    You won't create one of these objects. They're used to represent
    PEM-encoded data generated by `trustme`. For example, see `CA.cert_pem`,
    `LeafCert.private_key_and_cert_chain_pem`.

    """
    def __init__(self, data):
        self._data = data

    def bytes(self):
        """Returns the data as a `bytes` object.

        """
        return self._data

    def write_to_path(self, path, append=False):
        """Writes the data to the file at the given path.

        Args:
          path (str): The path to write to.
          append (bool): If False (the default), replace any existing file
               with the given name. If True, append to any existing file.

        """
        if append:
            mode = "ab"
        else:
            mode = "wb"
        with open(path, mode) as f:
            f.write(self._data)

    @contextmanager
    def tempfile(self, dir=None):
        """Context manager for writing data to a temporary file.

        The file is created when you enter the context manager, and
        automatically deleted when the context manager exits.

        Many libraries have annoying APIs which require that certificates be
        specified as filesystem paths, so even if you have already the data in
        memory, you have to write it out to disk and then let them read it
        back in again. If you encouter such a library, you should probably
        file a bug. But in the mean time, this context manager makes it easy
        to give them what they want.

        Example:

          Here's how to get requests to use a trustme CA (`see also
          <http://docs.python-requests.org/en/master/user/advanced/#ssl-cert-verification>`__)::

           ca = trustme.CA()
           with ca.cert_pem.tempfile() as ca_cert_path:
               requests.get("https://localhost/...", verify=ca_cert_path)

        Args:
          dir (str or None): Passed to `tempfile.NamedTemporaryFile`.

        """
        # On Windows, you can't re-open a NamedTemporaryFile that's still
        # open. Which seems like it completely defeats the purpose of having a
        # NamedTemporaryFile? Oh well...
        f = NamedTemporaryFile(suffix=".pem", dir=dir, delete=False)
        try:
            f.write(self._data)
            f.close()
            yield f.name
        finally:
            os.unlink(f.name)


class CA(object):
    """A certificate authority.

    Attributes:
      cert_pem (Blob): The PEM-encoded certificate for this CA. Add this to
          your trust store to trust this CA.

    """
    def __init__(self):
        self._private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=_KEY_SIZE,
            backend=default_backend()
        )

        self._certificate = (
            _cert_builder_common(
                _common_name(u"Testing CA"),
                _common_name(u"Testing CA"),
                self._private_key.public_key()
            )
            .add_extension(
                x509.BasicConstraints(ca=True, path_length=9), critical=True,
            )
            .sign(
                private_key=self._private_key,
                algorithm=hashes.SHA256(),
                backend=default_backend(),
            )
        )

        self.cert_pem = Blob(self._certificate.public_bytes(Encoding.PEM))

    def issue_server_cert(self, *hostnames):
        """Issues a server certificate.

        Args:
          *hostnames: The hostname or hostnames that this certificate will be
             valid for, as text (``unicode`` on Python 2, ``str`` on Python
             3).

        Returns:
          LeafCert

        """
        if not hostnames:
            raise ValueError("Must specify at least one hostname")

        key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=_KEY_SIZE,
            backend=default_backend()
        )

        ski = self._certificate.extensions.get_extension_for_class(
            x509.SubjectKeyIdentifier)

        cert = (
            _cert_builder_common(
                _common_name(u"Testing cert"),
                self._certificate.subject,
                key.public_key(),
            )
            .add_extension(
                x509.BasicConstraints(ca=False, path_length=None),
                critical=True,
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(
                    ski),
                critical=False,
            )
            .add_extension(
                x509.SubjectAlternativeName(
                    [x509.DNSName(h) for h in hostnames]
                ),
                critical=True,
            )
            .sign(
                private_key=self._private_key,
                algorithm=hashes.SHA256(),
                backend=default_backend(),
            )
        )
        return LeafCert(
                key.private_bytes(
                    Encoding.PEM,
                    PrivateFormat.TraditionalOpenSSL,
                    NoEncryption(),
                ),
                cert.public_bytes(Encoding.PEM),
            )
        
    def configure_trust(self, ctx):
        """Configure the given context object to trust certificates signed by
        this CA.

        Args:
          ctx (ssl.SSLContext or OpenSSL.SSL.Context): The SSL context to be
              modified.

        Returns:
          None: the context object is mutated in place.

        """
        if isinstance(ctx, ssl.SSLContext):
            ctx.load_verify_locations(
                cadata=self.cert_pem.bytes().decode("ascii"))
        elif _smells_like_pyopenssl(ctx):
            from OpenSSL import crypto
            cert = crypto.load_certificate(
                crypto.FILETYPE_PEM, self.cert_pem.bytes())
            store = ctx.get_cert_store()
            store.add_cert(cert)
        else:
            raise TypeError(
                "unrecognized context type {!r}"
                .format(ctx.__class__.__name__))


class LeafCert(object):
    """A server or client certificate.

    This object has no public constructor; you get one by calling
    `CA.issue_server_cert` or similar.

    Attributes:
      private_key_pem (Blob): The PEM-encoded private key corresponding to
          this certificate.

      cert_chain_pems (list of `Blob`\\s): The zeroth entry in this list is
          the actual PEM-encoded certificate, and any entries after that are
          the rest of the certificate chain needed to reach the root CA.

          Currently trustme doesn't have any support for intermediate CAs, so
          this list is always exactly one item long. But this way we're
          Future-Proof™.

      private_key_and_cert_chain_pem (Blob): A single `Blob` containing the
          concatenation of the PEM-encoded private key and the PEM-encoded
          cert chain.

    """
    def __init__(self, private_key_pem, server_cert_pem):
        self.private_key_pem = Blob(private_key_pem)
        self.cert_chain_pems = [Blob(server_cert_pem)]
        self.private_key_and_cert_chain_pem = (
            Blob(private_key_pem + server_cert_pem))

    def configure_cert(self, ctx):
        """Configure the given context object to present this certificate.

        Args:
          ctx (ssl.SSLContext or OpenSSL.SSL.Context): The SSL context to be
              modified.

        Returns:
          None: the context object is mutated in place.

        """
        if isinstance(ctx, ssl.SSLContext):
            # Currently need a temporary file for this, see:
            #   https://bugs.python.org/issue16487
            with self.private_key_and_cert_chain_pem.tempfile() as path:
                ctx.load_cert_chain(path)
        elif _smells_like_pyopenssl(ctx):
            from OpenSSL.crypto import (
                load_privatekey, load_certificate, FILETYPE_PEM,
            )
            key = load_privatekey(FILETYPE_PEM, self.private_key_pem.bytes())
            ctx.use_privatekey(key)
            cert = load_certificate(FILETYPE_PEM,
                                    self.cert_chain_pems[0].bytes())
            ctx.use_certificate(cert)
            # We don't actually have any way to create non-trivial cert chains
            # yet:
            assert len(self.cert_chain_pems) == 1
            # Probably it will want code something like:
            # for pem in self.cert_chain_pems[1:]:
            #     cert = load_certificate(FILETYPE_PEM, pem.bytes())
            #     ctx.add_extra_chain_cert(cert)
        else:
            raise TypeError(
                "unrecognized context type {!r}"
                .format(ctx.__class__.__name__))
