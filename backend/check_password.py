"""Dev helper: verify a seeded student's DOB-as-password against the stored hash."""
from app.database import SessionLocal, create_all

from app.models import Student
from app.student.dob import verify_dob

create_all()
db = SessionLocal()
s = db.query(Student).filter(Student.reg_no == 'CUS-2023-0001').first()
if s:
    print(f'Hash: {s.hashed_password[:20]}... (bcrypt, not < ={len(s.hashed_password)} chars >)')
    print(f'Verify DOB 15-Apr-2005 (seed format): {verify_dob("15-Apr-2005", s.hashed_password)}')
    print(f'Verify DOB 2005-04-15 (canonical):    {verify_dob("2005-04-15", s.hashed_password)}')
    print(f'Verify student123 (old model):        {verify_dob("student123", s.hashed_password)}')
else:
    print('Student CUS-2023-0001 not found — seed the DB first.')
db.close()