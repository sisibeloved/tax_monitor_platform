"""Generate an scrypt password hash for AUTH_LOCAL_ACCOUNTS."""

from getpass import getpass

from tax_risk.security.authentication import hash_password


def main() -> None:
    password = getpass("Password: ")
    confirmation = getpass("Confirm password: ")
    if password != confirmation:
        raise SystemExit("Passwords do not match")
    print(hash_password(password))


if __name__ == "__main__":
    main()
