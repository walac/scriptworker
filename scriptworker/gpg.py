#!/usr/bin/env python
"""GPG functions.  These currently assume gpg 2.0.x
"""
import arrow
import gnupg
import logging
import os
import pexpect
import pprint
import subprocess

from scriptworker.exceptions import ScriptWorkerGPGException

log = logging.getLogger(__name__)

# map the context.config keys to gnupg.GPG kwarg keys
GPG_CONFIG_MAPPING = {
    'gpg_home': 'gnupghome',
    'gpg_options': 'options',
    'gpg_path': 'gpgbinary',
    'gpg_public_keyring': 'keyring',
    'gpg_secret_keyring': 'secret_keyring',
    'gpg_use_agent': 'use_agent',
}


# helper functions {{{1
def gpg_default_args(gpg_home):
    """For commandline gpg calls, use these args by default
    """
    return [
        "--homedir", gpg_home,
        "--no-default-keyring",
        "--secret-keyring", os.path.join(gpg_home, "secring.gpg"),
        "--keyring", os.path.join(gpg_home, "pubring.gpg"),
    ]


def guess_gpg_home(obj, gpg_home=None):
    """Guess gpg_home.  If `gpg_home` is specified, return that.
    If `obj` is a context object and `context.config['gpg_home']` is not None,
    return that.
    If `obj` is a GPG object and `obj.gnupghome` is not None, return that.
    Otherwise look in `~/.gnupg`.  If os.environ['HOME'] isn't set, raise
    a ScriptWorkerGPGException.
    """
    try:
        if hasattr(obj, 'config'):
            gpg_home = gpg_home or obj.config['gpg_home']
        if hasattr(obj, 'gnupghome'):
            gpg_home = gpg_home or obj.gnupghome
        gpg_home = gpg_home or os.path.join(os.environ['HOME'], '.gnupg')
    except KeyError:
        raise ScriptWorkerGPGException("Can't guess_gpg_home: $HOME not set!")
    return gpg_home


def guess_gpg_path(context):
    """Simple gpg_path guessing function
    """
    return context.config['gpg_path'] or 'gpg'


# create_gpg_conf {{{1
def create_gpg_conf(homedir, keyservers=None, my_fingerprint=None):
    """ Set infosec guidelines; use my_fingerprint by default
    """
    gpg_conf = os.path.join(homedir, "gpg.conf")
    if os.path.exists(gpg_conf):
        os.rename(gpg_conf, "{}.{}".format(gpg_conf, arrow.utcnow().timestamp))
    with open(gpg_conf, "w") as fh:
        # https://wiki.mozilla.org/Security/Guidelines/Key_Management#GnuPG_settings
        print("personal-digest-preferences SHA512 SHA384\n"
              "cert-digest-algo SHA256\n"
              "default-preference-list SHA512 SHA384 AES256 ZLIB BZIP2 ZIP Uncompressed\n"
              "keyid-format 0xlong", file=fh)

        if keyservers:
            for keyserver in keyservers:
                print("keyserver {}".format(keyserver), file=fh)
            print("keyserver-options auto-key-retrieve", file=fh)

        if my_fingerprint is not None:
            # default key
            print("default-key {}".format(my_fingerprint), file=fh)


# GPG {{{1
def GPG(context, gpg_home=None):
    """Return a python-gnupg GPG instance
    """
    kwargs = {}
    for config_key, gnupg_key in GPG_CONFIG_MAPPING.items():
        if context.config[config_key] is not None:
            kwargs[gnupg_key] = context.config[config_key]
    if gpg_home is not None:
        kwargs['gnupghome'] = gpg_home
    gpg = gnupg.GPG(**kwargs)
    gpg.encoding = context.config['gpg_encoding'] or 'utf-8'
    return gpg


# key generation and export{{{1
def generate_key(gpg, name, comment, email, key_length=4096, expiration=None):
    """Generate a gpg keypair.
    """
    log.info("Generating key for {}...".format(email))
    kwargs = {
        "name_real": name,
        "name_comment": comment,
        "name_email": email,
        "key_length": key_length,
    }
    if expiration:
        kwargs['expire_date'] = expiration
    key = gpg.gen_key(gpg.gen_key_input(**kwargs))
    log.info("Fingerprint {}".format(key.fingerprint))
    return key.fingerprint


def export_key(gpg, fingerprint, private=False):
    """Return the ascii armored key identified by `fingerprint`.

    Raises ScriptworkerGPGException if the key isn't there.
    """
    message = "Exporting key {} from gnupghome {}".format(fingerprint, guess_gpg_home(gpg))
    log.info(message)
    key = gpg.export_keys(fingerprint, private)
    if not key:
        raise ScriptWorkerGPGException("Can't find key with fingerprint {}!".format(fingerprint))
    return key


def sign_key(context, target_fingerprint, signing_key=None, gpg_home=None):
    """Sign the `target_fingerprint` key with the `signing_key` or default key
    """
    args = []
    gpg_path = context.config['gpg_path'] or 'gpg'
    gpg_home = guess_gpg_home(context, gpg_home)
    message = "Signing key {} in {}".format(target_fingerprint, gpg_home)
    if signing_key:
        args.extend(['-u', signing_key])
        message += " with {}...".format(signing_key)
    log.info(message)
    # local, non-exportable signature
    args.append("--lsign-key")
    args.append(target_fingerprint)
    cmd_args = gpg_default_args(context.config['gpg_home']) + args
    child = pexpect.spawn(gpg_path, cmd_args)
    child.expect(b".*Really sign\? \(y/N\) ")
    child.sendline(b'y')
    index = child.expect([pexpect.EOF, pexpect.TIMEOUT])
    if index != 0:
        raise ScriptWorkerGPGException("Failed signing {}! Timeout".format(target_fingerprint))
    else:
        child.close()
        if child.exitstatus != 0 or child.signalstatus is not None:
            raise ScriptWorkerGPGException(
                "Failed signing {}! exit {} signal {}".format(
                    target_fingerprint, child.exitstatus, child.signalstatus
                )
            )


def update_trust(context, my_fingerprint, trusted_fingerprints=None, gpg_home=None):
    """ Trust my key ultimately; trusted_fingerprints fully
    """
    gpg_home = guess_gpg_home(context, gpg_home)
    log.info("Updating ownertrust in {}...".format(gpg_home))
    ownertrust = []
    trusted_fingerprints = trusted_fingerprints or []
    gpg_path = context.config['gpg_path'] or 'gpg'
    trustdb = os.path.join(gpg_home, "trustdb.gpg")
    if os.path.exists(trustdb):
        os.remove(trustdb)
    # trust my_fingerprint ultimately
    ownertrust.append("{}:6\n".format(my_fingerprint))
    # Trust trusted_fingerprints fully.  Once they are signed by my key, any
    # key they sign will be valid.  Only do this for root/intermediate keys
    # that are intended to sign other keys.
    for fingerprint in trusted_fingerprints:
        ownertrust.append("{}:5\n".format(fingerprint))
    log.debug(pprint.pformat(ownertrust))
    ownertrust = ''.join(ownertrust).encode('utf-8')
    cmd = [gpg_path] + gpg_default_args(gpg_home) + ["--import-ownertrust"]
    # TODO asyncio?
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stdin=subprocess.PIPE, stderr=subprocess.STDOUT)
    stdout = p.communicate(input=ownertrust)[0] or b''
    if p.returncode:
        raise ScriptWorkerGPGException("gpg update_trust error!\n{}".format(stdout.decode('utf-8')))
    log.debug(subprocess.check_output(
        [gpg_path] + gpg_default_args(gpg_home) + ["--export-ownertrust"]).decode('utf-8')
    )


# data signatures and verification {{{1
def sign(gpg, data, **kwargs):
    """Sign `data` with the key `kwargs['keyid']`, or the default key if not specified
    """
    return str(gpg.sign(data, **kwargs))


def verify_signature(gpg, signed_data, **kwargs):
    """Verify `signed_data` with the key `kwargs['keyid']`, or the default key
    if not specified.

    Raises ScriptWorkerGPGException on failure.
    """
    log.info("Verifying signature (gnupghome {})".format(guess_gpg_home(gpg)))
    verified = gpg.verify(signed_data, **kwargs)
    if verified.trust_level is not None and verified.trust_level >= verified.TRUST_FULLY:
        log.info("Fully trusted signature from {}, {}".format(verified.username, verified.key_id))
    else:
        raise ScriptWorkerGPGException("Signature could not be verified!")
    return verified


def get_body(gpg, signed_data, gpg_home=None, **kwargs):
    """Returned the unsigned data from `signed_data`.
    """
    verify_signature(gpg, signed_data)
    body = gpg.decrypt(signed_data, **kwargs)
    return str(body)


def parse_trust_line(trust_line, desc):
    """parse `gpg --list-sigs --with-colons` `tru` line

       https://git.gnupg.org/cgi-bin/gitweb.cgi?p=gnupg.git;a=blob;f=doc/DETAILS;h=645814a4c1fa8e8e735850f0f93b17617f60d4c8;hb=refs/heads/STABLE-BRANCH-2-0

       Example for a "tru" trust base record:

          tru:o:0:1166697654:1:3:1:5

        The fields are:

        2: Reason for staleness of trust.  If this field is empty, then the
           trustdb is not stale.  This field may have multiple flags in it:

           o: Trustdb is old
           t: Trustdb was built with a different trust model than the one we
              are using now.

        3: Trust model:
           0: Classic trust model, as used in PGP 2.x.
           1: PGP trust model, as used in PGP 6 and later.  This is the same
              as the classic trust model, except for the addition of trust
              signatures.

           GnuPG before version 1.4 used the classic trust model by default.
           GnuPG 1.4 and later uses the PGP trust model by default.

        4: Date trustdb was created in seconds since 1970-01-01.
        5: Date trustdb will expire in seconds since 1970-01-01.
        6: Number of marginally trusted users to introduce a new key signer
           (gpg's option --marginals-needed)
        7: Number of completely trusted users to introduce a new key signer.
           (gpg's option --completes-needed)
        8: Maximum depth of a certification chain.
           *gpg's option --max-cert-depth)
    """
    parts = trust_line.split(':')
    messages = []
    if parts[0] != 'tru':
        messages.append("{} is not a trust line!".format(trust_line))
    # check for staleness
    if parts[1]:
        messages.append(
            "{} trustdb is invalid! staleness '{}'\n"
            "o: Trustdb is old\n"
            "t: Trustdb was built with a different trust model than the one we\n"
            "are using now.".format(desc, parts[1])
        )
    # check for expiration; assuming 0 is no expiration
    if parts[4] and parts[4] < arrow.utcnow().timestamp:
        messages.append(
            "{} trustdb is expired, as of {}Z!".format(
                desc, arrow.get(parts[4]).format("YYYY-MM-DDTHH:mm:ssZZ")
            )
        )
    if messages:
        raise ScriptWorkerGPGException('\n'.join(messages))


def parse_pub_line(pub_line, desc):
    """parse `gpg --list-sigs --with-colons` `pub` line
    https://git.gnupg.org/cgi-bin/gitweb.cgi?p=gnupg.git;a=blob;f=doc/DETAILS;h=645814a4c1fa8e8e735850f0f93b17617f60d4c8;hb=refs/heads/STABLE-BRANCH-2-0
    2. Field:  A letter describing the calculated validity. This is a single
               letter, but be prepared that additional information may follow
               in some future versions. (not used for secret keys)
                   o = Unknown (this key is new to the system)
                   i = The key is invalid (e.g. due to a missing self-signature)
                   d = The key has been disabled
                       (deprecated - use the 'D' in field 12 instead)
                   r = The key has been revoked
                   e = The key has expired
                   - = Unknown validity (i.e. no value assigned)
                   q = Undefined validity
                       '-' and 'q' may safely be treated as the same
                       value for most purposes
                   n = The key is valid
                   m = The key is marginal valid.
                   f = The key is fully valid
                   u = The key is ultimately valid.  This often means
                       that the secret key is available, but any key may
                       be marked as ultimately valid.

               If the validity information is given for a UID or UAT
               record, it describes the validity calculated based on this
               user ID.  If given for a key record it describes the best
               validity taken from the best rated user ID.

               For X.509 certificates a 'u' is used for a trusted root
               certificate (i.e. for the trust anchor) and an 'f' for all
               other valid certificates.

    3. Field:  length of key in bits.

    4. Field:  Algorithm:  1 = RSA
                          16 = Elgamal (encrypt only)
                          17 = DSA (sometimes called DH, sign only)
                          20 = Elgamal (sign and encrypt - don't use them!)
               (for other id's see include/cipher.h)

    5. Field:  KeyID

    6. Field:  Creation Date (in UTC).  For UID and UAT records, this is
               the self-signature date.  Note that the date is usally
               printed in seconds since epoch, however, we are migrating
               to an ISO 8601 format (e.g. "19660205T091500").  This is
               currently only relevant for X.509.  A simple way to detect
               the new format is to scan for the 'T'.

    7. Field:  Key or user ID/user attribute expiration date or empty if none.

    8. Field:  Used for serial number in crt records (used to be the Local-ID).
               For UID and UAT records, this is a hash of the user ID contents
               used to represent that exact user ID.  For trust signatures,
               this is the trust depth seperated by the trust value by a
               space.

    9. Field:  Ownertrust (primary public keys only)
               This is a single letter, but be prepared that additional
               information may follow in some future versions.  For trust
               signatures with a regular expression, this is the regular
               expression value, quoted as in field 10.

   10. Field:  User-ID.  The value is quoted like a C string to avoid
               control characters (the colon is quoted "\x3a").
               For a "pub" record this field is not used on --fixed-list-mode.
               A UAT record puts the attribute subpacket count here, a
               space, and then the total attribute subpacket size.
               In gpgsm the issuer name comes here
               An FPR record stores the fingerprint here.
               The fingerprint of an revocation key is stored here.

   11. Field:  Signature class as per RFC-4880.  This is a 2 digit
               hexnumber followed by either the letter 'x' for an
               exportable signature or the letter 'l' for a local-only
               signature.  The class byte of an revocation key is also
               given here, 'x' and 'l' is used the same way.  IT is not
               used for X.509.

   12. Field:  Key capabilities:
                   e = encrypt
                   s = sign
                   c = certify
                   a = authentication
               A key may have any combination of them in any order.  In
               addition to these letters, the primary key has uppercase
               versions of the letters to denote the _usable_
               capabilities of the entire key, and a potential letter 'D'
               to indicate a disabled key.

   13. Field:  Used in FPR records for S/MIME keys to store the
               fingerprint of the issuer certificate.  This is useful to
               build the certificate path based on certificates stored in
               the local keyDB; it is only filled if the issuer
               certificate is available. The root has been reached if
               this is the same string as the fingerprint. The advantage
               of using this value is that it is guaranteed to have been
               been build by the same lookup algorithm as gpgsm uses.
               For "uid" records this lists the preferences in the same
               way the gpg's --edit-key menu does.
               For "sig" records, this is the fingerprint of the key that
               issued the signature.  Note that this is only filled in if
               the signature verified correctly.  Note also that for
               various technical reasons, this fingerprint is only
               available if --no-sig-cache is used.

   14. Field   Flag field used in the --edit menu output:

   15. Field   Used in sec/sbb to print the serial number of a token
               (internal protect mode 1002) or a '#' if that key is a
               simple stub (internal protect mode 1001)
   16. Field:  For sig records, this is the used hash algorithm:
                   2 = SHA-1
                   8 = SHA-256
               (for other id's see include/cipher.h)
    """
    pass


def parse_fpr_line(fpr_line, desc):
    """
 102 10. Field:  User-ID.  The value is quoted like a C string to avoid
 103             control characters (the colon is quoted "\x3a").
 104             For a "pub" record this field is not used on --fixed-list-mode.
 105             A UAT record puts the attribute subpacket count here, a
 106             space, and then the total attribute subpacket size.
 107             In gpgsm the issuer name comes here
 108             An FPR record stores the fingerprint here.
 109             The fingerprint of an revocation key is stored here.
    """
    pass


def list_key_signatures(context, key_fingerprint, gpg_home=None):
    """gpg --list-sigs, with machine parsable output, for gpg 2.0.x

  24  1. Field:  Type of record
  25             pub = public key
  26             crt = X.509 certificate
  27             crs = X.509 certificate and private key available
  28             sub = subkey (secondary key)
  29             sec = secret key
  30             ssb = secret subkey (secondary key)
  31             uid = user id (only field 10 is used).
  32             uat = user attribute (same as user id except for field 10).
  33             sig = signature
  34             rev = revocation signature
  35             fpr = fingerprint: (fingerprint is in field 10)
  36             pkd = public key data (special field format, see below)
  37             grp = keygrip
  38             rvk = revocation key
  39             tru = trust database information
  40             spk = signature subpacket
    """
    gpg_home = guess_gpg_home(context, gpg_home)
    gpg_path = guess_gpg_path(context)
    sig_output = subprocess.check_output(
        [gpg_path] + gpg_default_args(gpg_home) +
        ["--with-colons", "--list-sigs", "--with-fingerprint", "--with-fingerprint",
         key_fingerprint],
        stderr=subprocess.STDOUT
    )
    if "No public key" in sig_output:
        raise ScriptWorkerGPGException("No gpg key {} in {}!".format(key_fingerprint, gpg_home))
    return sig_output
