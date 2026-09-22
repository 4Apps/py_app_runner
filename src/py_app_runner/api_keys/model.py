from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar

from database_wrapper import DBDefaultsDataModel, MetadataDict, SerializeType
from database_wrapper_pgsql import PgCursorTypeAsync
from psycopg import sql


@dataclass
class ApiKeysModel(DBDefaultsDataModel):
    """One API key. Schema: `api_keys/files/install.pgsql.sql`.

    Subclass it to add columns or point at another table, and pass the subclass to
    `AppRegistry.configure(api_keys_model=...)`.
    """

    _defaults_config: ClassVar[list[str]] = ["created_at", "updated_at", "disabled_at"]

    @property
    def table_name(self) -> str:
        return "api_keys"

    name: str = field(
        default="",
        metadata=MetadataDict(db_field=("name", "text"), store=True, update=True),
    )
    key_prefix: str = field(
        default="",
        metadata=MetadataDict(db_field=("key_prefix", "text"), store=True, update=False),
    )
    secret_hash: str = field(
        default="",
        metadata=MetadataDict(db_field=("secret_hash", "text"), store=True, update=False, exclude=True),
    )
    abilities: list[str] = field(
        default_factory=list,
        metadata=MetadataDict(db_field=("abilities", "text[]"), store=True, update=True),
    )
    # psycopg returns cidr[] as ipaddress networks. None means any address.
    allowed_ips: list[Any] | None = field(
        default=None,
        metadata=MetadataDict(db_field=("allowed_ips", "cidr[]"), store=True, update=True),
    )
    expires_at: datetime | None = field(
        default=None,
        metadata=MetadataDict(
            db_field=("expires_at", "timestamptz"),
            store=True,
            update=True,
            serialize=SerializeType.DATETIME,
        ),
    )
    user_id: int | None = field(
        default=None,
        metadata=MetadataDict(db_field=("user_id", "bigint"), store=True, update=True),
    )
    last_used_at: datetime | None = field(
        default=None,
        metadata=MetadataDict(
            db_field=("last_used_at", "timestamptz"),
            store=False,
            update=True,
            serialize=SerializeType.DATETIME,
        ),
    )
    total_uses: int = field(
        default=0,
        metadata=MetadataDict(db_field=("total_uses", "bigint"), store=False, update=True),
    )

    async def use_key(self, db_cur: PgCursorTypeAsync) -> None:
        self.total_uses += 1
        self.last_used_at = datetime.now(UTC)

        await db_cur.execute(
            sql.SQL("UPDATE {table} SET last_used_at = now(), total_uses = total_uses + 1 WHERE id = %s").format(
                table=sql.Identifier(self.table_name)
            ),
            (self.id,),
        )
