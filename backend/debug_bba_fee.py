import sys
sys.path.insert(0, 'C:\\Users\\LENOVO\\OneDrive\\Desktop\\CUS_AI_BOT\\backend')

from app.catalogue.service import programme_by_id

# Check BBA programme
prog = programme_by_id('bba')
print('BBA programme:', prog)
if prog:
    print('fee_structure:', prog.get('fee_structure'))
    print('name:', prog.get('name'))
    print('code:', prog.get('code'))