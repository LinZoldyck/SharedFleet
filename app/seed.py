"""Jeu de données de démonstration :  python -m app.seed
Crée une société « Démo Utilitaires » (demo@sharedfleet.local / demonstration) avec 3 véhicules et 12 mois d'activité."""
import random
import secrets
from datetime import timedelta

from .db import SessionLocal, init_db
from .models import Booking, Customer, Expense, Fine, Message, Organization, User, Vehicle
from .security import hash_password
from .services import bookings as svc
from .services import settings
from .timeutil import now


def run() -> None:
    init_db()
    rnd = random.Random(7)
    with SessionLocal() as db:
        if db.query(User).filter_by(email="demo@sharedfleet.local").first():
            print("Les données de démonstration existent déjà.")
            return
        org = Organization(name="Démo Utilitaires", webhook_key=secrets.token_urlsafe(24), settings={})
        settings.update(org, {"company_name": "Démo Utilitaires SAS", "company_address": "12 rue des Entrepreneurs",
                              "company_postal_code": "69007", "company_city": "Lyon", "company_siret": "901 234 567 00012",
                              "company_phone": "04 72 00 00 00", "company_email": "contact@demo-utilitaires.fr"})
        db.add(org)
        db.flush()
        db.add(User(org_id=org.id, email="demo@sharedfleet.local", name="Camille Démo", role="admin",
                    password_hash=hash_password("demonstration")))
        t = now()
        created = t - timedelta(days=400)
        vehicles = [Vehicle(org_id=org.id, name=n, plate=p, brand=b, model=m, lock_code=c, daily_rate=r, monthly_cost=mc,
                            address="12 rue des Entrepreneurs, 69007 Lyon", getaround_car_id=1000 + i, created_at=created)
                    for i, (n, p, b, m, c, r, mc) in enumerate([
                        ("Master 12 m³", "GH-482-KL", "Renault", "Master L2H2", "4821", 89, 520),
                        ("Trafic 6 m³", "FT-915-PA", "Renault", "Trafic L1H1", "7350", 69, 430),
                        ("Boxer 20 m³", "GM-207-ZE", "Peugeot", "Boxer caisse hayon", "1946", 119, 640)])]
        db.add_all(vehicles)
        pros = [Customer(org_id=org.id, company_name=cn, siret=s, first_name=f, last_name=l, phone=ph, email=e,
                         address=a, postal_code="69003", city="Lyon", license_number=lic)
                for cn, s, f, l, ph, e, a, lic in [
                    ("Lumen Événementiel", "852 369 741 00025", "Inès", "Moreau", "06 12 34 56 78", "ines@lumen-event.fr", "4 quai Sarrail", "13AB45678"),
                    ("Atelier Bois & Co", "798 456 123 00018", "Hugo", "Lefèvre", "06 98 76 54 32", "hugo@atelierbois.fr", "27 rue Paul Bert", "09CD12345"),
                    ("Vélocité Livraison", "889 123 456 00031", "Sarah", "Benali", "07 45 12 78 96", "sarah@velocite.fr", "8 cours Lafayette", "15EF98765")]]
        drivers = [Customer(org_id=org.id, first_name=f, last_name=l, phone="06 00 00 00 0%d" % i, address="3 rue de la Paix",
                            postal_code="69002", city="Lyon", license_number=f"1{i}GA5567{i}", getaround_user_id=5000 + i)
                   for i, (f, l) in enumerate([("Léa", "Martin"), ("Tom", "Girard"), ("Nina", "Roux"), ("Paul", "Faure")])]
        db.add_all(pros + drivers)
        db.flush()

        # 12 mois d'historique : ~80 % Getaround, ~20 % direct
        for v in vehicles:
            cursor = t - timedelta(days=350)
            while cursor < t + timedelta(days=12):
                cursor += timedelta(hours=rnd.choice([10, 20, 30, 50, 70, 96]))
                start = cursor.replace(minute=0, second=0, hour=rnd.choice([7, 8, 9, 14]))
                length = rnd.choice([6, 10, 24, 34, 58, 72])
                end = start + timedelta(hours=length)
                direct = rnd.random() < 0.2
                b = Booking(org_id=org.id, vehicle_id=v.id, starts_at=start, ends_at=end, created_at=start - timedelta(days=3),
                            source="direct" if direct else "getaround", vat_rate=20 if direct else 0)
                b.vehicle = v
                b.customer_id = rnd.choice(pros).id if direct else rnd.choice(drivers).id
                b.price_ht = round(v.daily_rate * b.days * (1 if direct else rnd.uniform(0.62, 0.8)), 2)
                b.km_included = v.km_included * b.days
                b.deposit = v.deposit if direct else 0
                b.status = "completed" if end < t else "in_progress" if start <= t else "confirmed"
                if direct:
                    b.access_code = svc.new_access_code(db)
                else:
                    b.external_id = str(rnd.randint(10**6, 10**7))
                db.add(b)
                db.flush()
                cursor = end
            for m in range(12):
                day = (t - timedelta(days=30 * m + rnd.randint(1, 25))).date()
                db.add(Expense(org_id=org.id, vehicle_id=v.id, spent_on=day, category=rnd.choice(["Entretien", "Carburant", "Nettoyage", "Pneus"]),
                               label="Dépense de démonstration", amount=round(rnd.uniform(40, 320), 2)))
        db.flush()
        past = db.query(Booking).filter(Booking.org_id == org.id, Booking.status == "completed").order_by(Booking.starts_at.desc())
        ga = past.filter_by(source="getaround").first()
        di = past.filter_by(source="direct").first()
        live = db.query(Booking).filter(Booking.org_id == org.id, Booking.source == "getaround", Booking.status != "completed").first()
        db.add_all([
            Fine(org_id=org.id, file_path="", file_hash="demo1", kind="amende", notice_number="4012345678", plate=ga.vehicle.plate,
                 offense_at=ga.starts_at + timedelta(hours=2), offense_type="Excès de vitesse inférieur à 20 km/h", location="A7 — Pierre-Bénite",
                 amount=68, vehicle_id=ga.vehicle_id, booking_id=ga.id, status="matched", extraction="claude"),
            Fine(org_id=org.id, file_path="", file_hash="demo2", kind="fps", notice_number="26069123456789012345", plate=di.vehicle.plate,
                 offense_at=di.starts_at + timedelta(hours=3), offense_type="Stationnement payant non réglé", location="Lyon 3e — rue Garibaldi",
                 amount=35, vehicle_id=di.vehicle_id, booking_id=di.id, status="matched", extraction="claude"),
        ])
        if live:
            db.add_all([
                Message(org_id=org.id, booking_id=live.id, direction="in", external_id="d1", sent_at=t - timedelta(hours=3), status="draft",
                        content="Bonjour, le véhicule passe-t-il sous une barrière de 2,60 m ? Et faut-il un permis spécial ?",
                        ai_reason="Question factuelle couverte par les consignes du loueur.",
                        ai_draft="Bonjour, le véhicule mesure 2,50 m de haut : il passe sous une barrière de 2,60 m. Le permis B suffit. Bonne location !"),
                Message(org_id=org.id, booking_id=live.id, direction="in", external_id="d2", sent_at=t - timedelta(hours=1), status="escalated",
                        content="Serait-il possible de rendre le véhicule 3 heures plus tard sans supplément ?",
                        ai_reason="Demande de prolongation et de geste commercial : décision du loueur."),
            ])
        db.commit()
        print("Démo prête : demo@sharedfleet.local / demonstration")


if __name__ == "__main__":
    run()
