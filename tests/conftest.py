import io
import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="sharedfleet-test-")
os.environ["DISABLE_SCHEDULER"] = "true"
os.environ["SECRET_KEY"] = "test-secret"
os.environ["BASE_URL"] = "http://testserver"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

from app import security  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402

ORIGIN = {"Origin": "http://testserver"}


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    security._attempts.clear()
    yield


@pytest.fixture
def db():
    with SessionLocal() as session:
        yield session


def new_client() -> TestClient:
    return TestClient(app, headers=ORIGIN, follow_redirects=False)


@pytest.fixture
def client():
    """Navigateur connecté en administrateur d'une société fraîchement créée."""
    c = new_client()
    r = c.post("/inscription", data={"company": "Utilitaires Test", "name": "Alice Admin", "email": "alice@test.fr",
                                     "password": "motdepasse-solide"})
    assert r.status_code == 303
    return c


def photo(color="#778899") -> tuple[str, bytes, str]:
    buf = io.BytesIO()
    Image.new("RGB", (640, 480), color).save(buf, "JPEG")
    return ("photo.jpg", buf.getvalue(), "image/jpeg")


@pytest.fixture
def fleet(client, db):
    """Un véhicule et un client professionnel."""
    from app.models import Customer, Vehicle

    client.post("/vehicules/0", data={"name": "Master", "plate": "ab-123-cd", "brand": "Renault", "model": "Master",
                                      "lock_code": "4821", "daily_rate": "90", "km_included": "200", "extra_km_price": "0,30",
                                      "deposit": "1000", "monthly_cost": "500", "active": "on"})
    client.post("/clients/0", data={"company_name": "Lumen Events", "siret": "85236974100025", "first_name": "Inès",
                                    "last_name": "Moreau", "phone": "06 12 34 56 78", "email": "ines@lumen.fr",
                                    "address": "4 quai Sarrail", "postal_code": "69003", "city": "Lyon"})
    return db.query(Vehicle).one(), db.query(Customer).one()
