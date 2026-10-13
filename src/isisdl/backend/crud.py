from __future__ import annotations

import asyncio
import os

from cryptography.fernet import Fernet
from sqlalchemy import select, delete
from sqlalchemy.orm import Session as DatabaseSession

from isisdl.api.crud import authenticate_with_password
from isisdl.api.endpoints import UserIDAPI
from isisdl.backend.crypt import get_credentials
from isisdl.backend.models import User, Config, generate_key, UpdatePolicy
from isisdl.db_conf import add_object_to_database, DatabaseSessionMaker
from isisdl.settings import master_password, error_exit, env_var_name_username, env_var_name_password
from isisdl.utils import config as legacy


def store_user(db: DatabaseSession, username: str, password: str, user_id: int, password_to_encrypt: str | None, config: Config) -> User | None:
    the_password_to_encrypt = password_to_encrypt if config.pw_encrypt_password else master_password
    if the_password_to_encrypt is None:
        return None

    key = generate_key(the_password_to_encrypt, config)
    encrypted_password = Fernet(key).encrypt(password.encode()).decode()

    return add_object_to_database(db, User(username=username, encrypted_password=encrypted_password, user_id=user_id))


def create_default_config(db: DatabaseSession) -> Config | None:
    return add_object_to_database(db, Config(database_version=1))


def read_user(db: DatabaseSession) -> User | None:
    return db.execute(select(User)).scalar()


def read_config(db: DatabaseSession) -> Config:
    configs = db.execute(select(Config)).scalars().all()

    if len(configs) != 1:
        error_exit(2, f"Could not load config! Got {len(configs)} config database entries, expected 1\nPlease make sure you have set the config with `isisdl --init`!")

    return configs[0]


legacy_update_policies = {
    None: UpdatePolicy.none,
    "install_pip": UpdatePolicy.pip, "notify_pip": UpdatePolicy.pip,
    "install_github": UpdatePolicy.github, "notify_github": UpdatePolicy.github,
}


def create_config_from_legacy(db: DatabaseSession) -> Config | None:
    """
    Creates the config for the new database from the 1.3 config.
    Options that have no counterpart in the new config yet (whitelist, blacklist, renamed courses, timer) are left out.
    """
    return add_object_to_database(db, Config(
        database_version=1,
        send_logs_to_server=bool(legacy.telemetry_policy),
        update_policy=legacy_update_policies.get(legacy.update_policy, UpdatePolicy.none),
        fs_make_subdirs=bool(legacy.make_subdirs),
        fs_sanitize_filenames=bool(legacy.filename_replacing),
        dl_download_videos=bool(legacy.download_videos),
        dl_follow_links=bool(legacy.follow_links),
        dl_throttle_rate=legacy.throttle_rate,
        dl_throttle_rate_autorun=legacy.throttle_rate_autorun,
        dl_show_full_path=bool(legacy.absolute_path_filename),
    ))


async def store_user_with_lookup(db: DatabaseSession, username: str, password: str, config: Config) -> User | None:
    """
    Logs in to look up the ISIS user id, which is the primary key of the user, and stores the user.
    """
    session = await authenticate_with_password(username, password)
    if session is None:
        return None

    try:
        user_id = await UserIDAPI.get(session)
    finally:
        await session.close()

    if user_id is None:
        return None

    return store_user(db, username, password, user_id, None, config)


def delete_users(db: DatabaseSession) -> None:
    db.execute(delete(User))
    db.commit()


async def read_or_migrate_user(db: DatabaseSession, config: Config) -> User | None:
    """
    Returns the user from the new database. If there is none yet, the credentials of the 1.3 config are migrated
    (environment variables take precedence, see `get_credentials`).
    Passphrase-protected credentials are not migrated, since the new config cannot store them yet.
    """
    user = read_user(db)
    if user is not None:
        return user

    if legacy.password_encrypted and not (os.getenv(env_var_name_username) and os.getenv(env_var_name_password)):
        return None

    legacy_user = get_credentials()
    return await store_user_with_lookup(db, legacy_user.username, legacy_user.password, config)


def read_or_create_config(db: DatabaseSession) -> Config:
    configs = db.execute(select(Config)).scalars().all()
    if not configs:
        config = create_config_from_legacy(db)
        if config is None:
            error_exit(2, "Could not create the config in the database!")

        return config

    return read_config(db)


def replace_user_in_new_database(username: str | None, password: str | None, passphrase: str | None = None) -> None:
    """
    Mirrors the credentials that the 1.3 wizard stores into the new database.
    The new config cannot store passphrase-protected credentials yet, so those (and an unset password) only remove the stored user.
    """
    with DatabaseSessionMaker() as db:
        config = read_or_create_config(db)
        delete_users(db)

        if username is None or password is None or passphrase:
            return

        asyncio.run(store_user_with_lookup(db, username, password, config))
