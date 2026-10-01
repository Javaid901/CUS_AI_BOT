import sys
sys.path.insert(0, 'C:\\Users\\LENOVO\\OneDrive\\Desktop\\CUS_AI_BOT\\backend')

from app.catalogue.service import resolve_programme, programme_by_id

# Check BCA programme
resolved = resolve_programme('bca')
print('resolved:', resolved)
if resolved:
    prog = programme_by_id(resolved['id'])
    print('fee_structure:', prog.get('fee_structure'))