import sys
sys.path.insert(0, 'C:\\Users\\LENOVO\\OneDrive\\Desktop\\CUS_AI_BOT\\backend')

from app.catalogue.service import resolve_programme, programme_by_id

# Check BBA programme via resolve
prog = resolve_programme('bba')
print('resolve_programme bba:', prog)
if prog:
    prog2 = programme_by_id(prog['id'])
    print('programme_by_id with uuid:', prog2)
    if prog2:
        print('fee_structure:', prog2.get('fee_structure'))