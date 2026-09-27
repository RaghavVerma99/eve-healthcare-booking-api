import asyncio
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy import func, select

from app.core.config import settings
from app.core.security import hash_password
from app.db.base import Base
from app.db.session import SessionLocal, engine
from app.models import CentreTest, DiagnosticCentre, DiagnosticTest, User
from app.schemas.catalogue import CentreCreate, TestCreate

CENTRES: list[dict] = [
    {
        "centre": {
            "name": "EVE City Diagnostics",
            "address": "12 MG Road, Bengaluru, Karnataka 560001",
            "city": "Bengaluru",
            "state": "Karnataka",
            "postal_code": "560001",
            "phone": "+918000000001",
            "latitude": Decimal("12.971599"),
            "longitude": Decimal("77.594566"),
        },
        "tests": [
            ("CBC", 500),
            ("LIPID", 950),
            ("TFT", 700),
            ("HBA1C", 650),
            ("URINE_R", 250),
        ],
    },
    {
        "centre": {
            "name": "EVE Airport Diagnostics",
            "address": "4 Airport Road, Bengaluru, Karnataka 560017",
            "city": "Bengaluru",
            "state": "Karnataka",
            "postal_code": "560017",
            "phone": "+918000000002",
            "latitude": Decimal("13.198600"),
            "longitude": Decimal("77.706600"),
        },
        "tests": [
            ("CBC", 550),
            ("LIPID", 990),
            ("TFT", 720),
        ],
    },
    {
        "centre": {
            "name": "EVE Pune Diagnostics",
            "address": "9 FC Road, Pune, Maharashtra 411005",
            "city": "Pune",
            "state": "Maharashtra",
            "postal_code": "411005",
            "phone": "+912000000003",
            "latitude": Decimal("18.537500"),
            "longitude": Decimal("73.853600"),
        },
        "tests": [
            ("CBC", 480),
            ("HBA1C", 600),
        ],
    },
]

TESTS: list[dict] = [
    {
        "code": "CBC",
        "name": "Complete Blood Count",
        "description": "Haemoglobin, WBC, RBC and platelet counts.",
        "duration_minutes": 30,
        "base_price": Decimal("450.00"),
        "fasting_required": False,
    },
    {
        "code": "LIPID",
        "name": "Lipid Profile",
        "description": "Total cholesterol, HDL, LDL and triglycerides.",
        "duration_minutes": 45,
        "base_price": Decimal("900.00"),
        "fasting_required": True,
    },
    {
        "code": "TFT",
        "name": "Thyroid Function Test",
        "description": "T3, T4 and TSH hormone levels.",
        "duration_minutes": 45,
        "base_price": Decimal("650.00"),
        "fasting_required": False,
    },
    {
        "code": "HBA1C",
        "name": "Glycated Haemoglobin",
        "description": "Average blood glucose over the last three months.",
        "duration_minutes": 30,
        "base_price": Decimal("600.00"),
        "fasting_required": False,
    },
    {
        "code": "URINE_R",
        "name": "Urine Routine",
        "description": "Protein, glucose and pH screening.",
        "duration_minutes": 20,
        "base_price": Decimal("200.00"),
        "fasting_required": False,
    },
]


async def seed() -> None:
    def _table_names(sync_connection) -> set[str]:
        return set(sa.inspect(sync_connection).get_table_names())

    async with engine.begin() as connection:
        table_names = await connection.run_sync(_table_names)
    missing = {table.name for table in Base.metadata.sorted_tables} - table_names
    if missing:
        raise SystemExit(
            "Database schema is missing "
            f"{len(missing)} table(s) (e.g. {sorted(missing)[:3]}). "
            "Run `alembic upgrade head` (or `make migrate`) before seeding."
        )

    async with SessionLocal() as session:
        existing_tests = {code for (code,) in (await session.execute(select(DiagnosticTest.code)))}
        created_tests: dict[str, DiagnosticTest] = {}
        for payload in TESTS:
            validated = TestCreate(**payload)
            if validated.code in existing_tests:
                created_tests[validated.code] = await session.scalar(
                    select(DiagnosticTest).where(DiagnosticTest.code == validated.code)
                )
                continue
            test = DiagnosticTest(**validated.model_dump())
            session.add(test)
            created_tests[validated.code] = test
        await session.flush()

        for entry in CENTRES:
            payload = CentreCreate(**entry["centre"])
            centre = await session.scalar(
                select(DiagnosticCentre).where(DiagnosticCentre.name == payload.name)
            )
            if centre is None:
                centre = DiagnosticCentre(**payload.model_dump())
                session.add(centre)
                await session.flush()

            for code, price in entry["tests"]:
                test = created_tests[code]
                offering = await session.get(
                    CentreTest, {"centre_id": centre.id, "test_id": test.id}
                )
                if offering is None:
                    session.add(
                        CentreTest(
                            centre_id=centre.id,
                            test_id=test.id,
                            price=Decimal(str(price)),
                        )
                    )

        admin_email = settings.seed_admin_email.lower()
        admin = await session.scalar(
            select(User).where(func.lower(User.email) == admin_email)
        )
        if admin is None:
            session.add(
                User(
                    email=admin_email,
                    full_name="EVE Administrator",
                    hashed_password=hash_password(settings.seed_admin_password),
                    is_admin=True,
                )
            )
        await session.commit()

        centre_count = await session.scalar(select(func.count()).select_from(DiagnosticCentre))
        test_count = await session.scalar(select(func.count()).select_from(DiagnosticTest))
        print(
            f"seeded: {centre_count} centres, {test_count} tests, "
            f"admin user '{admin_email}'"
        )


if __name__ == "__main__":
    asyncio.run(seed())
