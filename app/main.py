"""Employee Attendance & Analytics API. Run: uvicorn app.main:app --port 8000"""
import os, re, calendar
from bson import ObjectId
from datetime import datetime, date, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError
from bson import json_util

load_dotenv()
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
client = MongoClient(os.getenv('MONGO_URI', 'mongodb://localhost:27017'), tz_aware=True, serverSelectionTimeoutMS=3000)
db = client[os.getenv('MONGO_DB', 'attendance_db')]
app = FastAPI(title='Employee Attendance & Analytics API', version='2.0.0')
PRESENCE = {'PRESENT','WFH','ON_DUTY'}
STATUSES = PRESENCE | {'ABSENT','LEAVE'}

def make_json_safe(value):
    if isinstance(value, ObjectId):
        return str(value)

    if isinstance(value, dict):
        return {
            key: make_json_safe(item)
            for key, item in value.items()
        }

    if isinstance(value, list):
        return [make_json_safe(item) for item in value]

    return value


def now_utc():
    return datetime.now(UTC).replace(microsecond=0)
def epoch_ms(dt):
    if dt is None: return None
    if dt.tzinfo is None: dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)
def from_ms(ms):
    if type(ms) is not int or not 100_000_000_000 <= ms <= 4_102_444_800_000:
        raise ValueError('timestamp must be epoch milliseconds')
    return datetime.fromtimestamp(ms // 1000, UTC)
def iso_date(value):
    try: return date.fromisoformat(value).isoformat()
    except Exception: raise ValueError('date must be YYYY-MM-DD')
def time_parts(s):
    if not isinstance(s,str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',s): raise ValueError('time must be HH:MM')
    h,m=map(int,s.split(':')); return h,m
def shift_start_dt(day, shift_start):
    h,m=time_parts(shift_start); return datetime.combine(day,time(h,m),IST).astimezone(UTC)
def shift_end_dt(day, shift_start, shift_end):
    sh=time_parts(shift_start); eh=time_parts(shift_end)
    end_day = day + timedelta(days=1) if eh <= sh else day
    return datetime.combine(end_day,time(*eh),IST).astimezone(UTC)
def attendance_day(ts, emp):
    local=ts.astimezone(IST); day=local.date()
    if time_parts(emp['shift_end']) <= time_parts(emp['shift_start']) and local.time().replace(tzinfo=None) < time(*time_parts(emp['shift_end'])):
        day -= timedelta(days=1)
    return day.isoformat()
def late_minutes(ts, emp, day):
    start=shift_start_dt(date.fromisoformat(day),emp['shift_start'])
    seconds=int((ts-start).total_seconds())
    return seconds//60 if seconds > 600 else 0
def work_hours(start,end):
    seconds=Decimal(str(int((end-start).total_seconds())))
    return float((seconds/Decimal(3600)).quantize(Decimal('0.01'),rounding=ROUND_HALF_UP))
def overtime_minutes(end, emp, day):
    shift_end=shift_end_dt(date.fromisoformat(day),emp['shift_start'],emp['shift_end'])
    mins=max(0,int((end-shift_end).total_seconds())//60)
    return mins if mins >= 30 else 0
def derived(emp, day, status, pin, pout):
    if status not in PRESENCE: return {'work_hours':None,'late_minutes':0,'overtime_minutes':0,'half_day':False}
    late=late_minutes(pin,emp,day) if pin else 0
    if pin and pout:
        hours=work_hours(pin,pout)
        return {'work_hours':hours,'late_minutes':late,'overtime_minutes':overtime_minutes(pout,emp,day),'half_day':hours < 4.50}
    return {'work_hours':None,'late_minutes':late,'overtime_minutes':0,'half_day':False}
def serialize(doc):
    if not doc:return None
    out={k:v for k,v in doc.items() if k!='_id'}
    for f in ('punch_in','punch_out'):
        out[f]=epoch_ms(out.get(f))
    hist=[]
    for h in out.get('history',[]):
        entry={'at':epoch_ms(h['at']),'by':h['by'],'reason':h['reason'],'changes':{}}
        for k,v in h.get('changes',{}).items():
            entry['changes'][k]={'from':epoch_ms(v['from']) if k in ('punch_in','punch_out') else v['from'], 'to':epoch_ms(v['to']) if k in ('punch_in','punch_out') else v['to']}
        hist.append(entry)
    out['history']=hist
    if 'created_at' in out: out['created_at']=epoch_ms(out['created_at'])
    return out
def date_range_month(month):
    if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])',month): raise HTTPException(422,'month must be YYYY-MM')
    y,m=map(int,month.split('-')); return f'{month}-01', f'{month}-{calendar.monthrange(y,m)[1]:02d}'
def half_up(value, places=2):
    if value is None:return None
    return float(Decimal(str(value)).quantize(Decimal('1.'+'0'*places),rounding=ROUND_HALF_UP))
def page_params(page:int,page_size:int):
    if page<1 or not 1<=page_size<=100: raise HTTPException(422,'page must be >=1 and page_size must be 1..100')
    return (page-1)*page_size


class EmployeeIn(BaseModel):
    model_config=ConfigDict(extra='ignore')
    emp_code:str=Field(pattern=r'^EMP\d{4,6}$'); name:str=Field(min_length=1,max_length=100)
    email:str=Field(max_length=120,pattern=r'^[^@\s]+@[^@\s]+\.[^@\s]+$'); department:str=Field(min_length=1,max_length=50)
    shift_start:str='09:30'; shift_end:str='18:30'; joined_on:str
    @field_validator('shift_start','shift_end')
    @classmethod
    def valid_time(cls,v): time_parts(v); return v
    @field_validator('joined_on')
    @classmethod
    def valid_join(cls,v): return iso_date(v)
    @model_validator(mode='after')
    def shifts_differ(self):
        if self.shift_start==self.shift_end: raise ValueError('shift_start must differ from shift_end')
        return self
class PunchInIn(BaseModel):
    model_config=ConfigDict(extra='ignore')
    emp_code:str; punched_at:Optional[int]=Field(default=None,ge=100_000_000_000,le=4_102_444_800_000); status:Literal['PRESENT','WFH','ON_DUTY']='PRESENT'
class PunchOutIn(BaseModel):
    model_config=ConfigDict(extra='ignore')
    emp_code:str; punched_at:Optional[int]=Field(default=None,ge=100_000_000_000,le=4_102_444_800_000)
class RegularizeIn(BaseModel):
    model_config=ConfigDict(extra='ignore')
    status:Optional[Literal['PRESENT','WFH','ON_DUTY','ABSENT','LEAVE']]=None
    punch_in:Optional[int]=Field(default=None,ge=100_000_000_000,le=4_102_444_800_000)
    punch_out:Optional[int]=Field(default=None,ge=100_000_000_000,le=4_102_444_800_000)
    reason:str=Field(min_length=5,max_length=200); regularized_by:str=Field(min_length=1,max_length=50)
    @model_validator(mode='before')
    @classmethod
    def require_change_field(cls,v):
        if isinstance(v,dict) and not any(k in v for k in ('status','punch_in','punch_out')): raise ValueError('at least one attendance field is required')
        return v

@app.on_event('startup')
def ensure_indexes():
    db.employees.create_index([('emp_code',ASCENDING)],unique=True,name='uq_employee_code')
    db.employees.create_index([('department',ASCENDING),('joined_on',ASCENDING)],name='department_joined')
    db.attendance_logs.create_index([('emp_code',ASCENDING),('date',ASCENDING)],unique=True,name='uq_attendance_employee_date')
    db.attendance_logs.create_index([('date',DESCENDING),('emp_code',ASCENDING)],name='attendance_date_employee')
    db.attendance_logs.create_index([('date',ASCENDING),('status',ASCENDING),('emp_code',ASCENDING)],name='attendance_date_status_employee')

@app.get('/health')
def health():
    try: db.command('ping'); return {'status':'ok'}
    except Exception: raise HTTPException(503,'database unavailable')

@app.post('/employees',status_code=201)
def create_employee(body:EmployeeIn):
    doc=body.model_dump(); doc['created_at']=now_utc()
    try: db.employees.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409,'emp_code already exists')
    response = {
        k: (epoch_ms(v) if k == 'created_at' else v)
        for k, v in doc.items()
    }

    return make_json_safe(response)

@app.get('/employees')
def list_employees(department:Optional[str]=None,page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100)):
    q={'department':department} if department is not None else {}; total=db.employees.count_documents(q)
    docs=list(db.employees.find(q,{'_id':0}).sort('emp_code',ASCENDING).skip(page_params(page,page_size)).limit(page_size))
    for d in docs: d['created_at']=epoch_ms(d['created_at']) if d.get('created_at') else None
    return {'items':docs,'total':total,'page':page,'page_size':page_size}

@app.post('/attendance/punch-in',status_code=201)
def punch_in(body:PunchInIn):
    emp=db.employees.find_one({'emp_code':body.emp_code})
    if not emp: raise HTTPException(404,'employee not found')
    ts=from_ms(body.punched_at) if body.punched_at is not None else now_utc(); day=attendance_day(ts,emp)
    doc={'emp_code':body.emp_code,'date':day,'status':body.status,'punch_in':ts,'punch_out':None,'work_hours':None,'late_minutes':late_minutes(ts,emp,day),'overtime_minutes':0,'half_day':False,'history':[]}
    try: db.attendance_logs.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409,'already punched in for this date')
    return serialize(doc)

@app.post('/attendance/punch-out')
def punch_out(body:PunchOutIn):
    emp=db.employees.find_one({'emp_code':body.emp_code})
    if not emp: raise HTTPException(404,'employee not found')
    ts=from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    doc=db.attendance_logs.find_one({'emp_code':body.emp_code,'punch_in':{'$lte':ts}},sort=[('punch_in',DESCENDING)])
    if not doc: raise HTTPException(404,'no punch-in found')
    if doc.get('punch_out') is not None: raise HTTPException(409,'record already punched out')
    secs=(ts-doc['punch_in']).total_seconds()
    if secs<=0 or secs>86400: raise HTTPException(422,'punch-out must be after punch-in and within 24 hours')
    calc=derived(emp,doc['date'],doc['status'],doc['punch_in'],ts)
    updated=db.attendance_logs.find_one_and_update({'_id':doc['_id'],'punch_out':None}, {'$set':{'punch_out':ts,**calc}},return_document=ReturnDocument.AFTER)
    if not updated: raise HTTPException(409,'record already punched out')
    return serialize(updated)

@app.get('/attendance')
def list_attendance(emp_code:Optional[str]=None,date_from:Optional[str]=None,date_to:Optional[str]=None,status:Optional[Literal['PRESENT','ABSENT','LEAVE','WFH','ON_DUTY']]=None,page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100)):
    if date_from:
        try: iso_date(date_from)
        except ValueError as e: raise HTTPException(422,str(e))
    if date_to:
        try: iso_date(date_to)
        except ValueError as e: raise HTTPException(422,str(e))
    if date_from and date_to and date_from>date_to: raise HTTPException(422,'date_from must be <= date_to')
    q={}
    if emp_code:q['emp_code']=emp_code
    if status:q['status']=status
    if date_from or date_to:
        q['date']={}
        if date_from:q['date']['$gte']=date_from
        if date_to:q['date']['$lte']=date_to
    offset=page_params(page,page_size); total=db.attendance_logs.count_documents(q)
    docs=list(db.attendance_logs.find(q,{'_id':0}).sort([('date',DESCENDING),('emp_code',ASCENDING)]).skip(offset).limit(page_size))
    return {'items':[serialize(d) for d in docs],'total':total,'page':page,'page_size':page_size}

@app.patch('/attendance/{emp_code}/{date}')
def regularize(emp_code:str,date:str,body:RegularizeIn):
    emp = db.employees.find_one({'emp_code': emp_code})
    doc = db.attendance_logs.find_one({
    'emp_code': emp_code,
    'date': date
})
    print("Employee found:", emp is not None)
    print("Attendance found:", doc is not None)
    print("Received employee code:", repr(emp_code))
    print("Received date:", repr(date))
    try: iso_date(date)
    except ValueError as e: raise HTTPException(422,str(e))
    emp=db.employees.find_one({'emp_code':emp_code}); doc=db.attendance_logs.find_one({'emp_code':emp_code,'date':date})
    if not emp or not doc: raise HTTPException(404,'employee or attendance record not found')
    status=body.status if body.status is not None else doc['status']
    pin=from_ms(body.punch_in) if body.punch_in is not None else doc.get('punch_in')
    pout=from_ms(body.punch_out) if body.punch_out is not None else doc.get('punch_out')
    
    if 'punch_in' in body.model_fields_set: pin=from_ms(body.punch_in) if body.punch_in is not None else None
    if 'punch_out' in body.model_fields_set: pout=from_ms(body.punch_out) if body.punch_out is not None else None
    if status in ('ABSENT','LEAVE'):
        if ('punch_in' in body.model_fields_set and body.punch_in is not None) or ('punch_out' in body.model_fields_set and body.punch_out is not None): raise HTTPException(422,'ABSENT/LEAVE cannot have punch times')
        pin=pout=None
    elif pin is None: raise HTTPException(422,'presence status requires punch_in')
    if pin is not None and attendance_day(pin,emp)!=date: raise HTTPException(422,'punch_in must belong to attendance date')
    if pout is not None:
        if pin is None or (pout-pin).total_seconds()<=0 or (pout-pin).total_seconds()>86400: raise HTTPException(422,'punch_out must be after punch_in and within 24 hours')
    calc=derived(emp,date,status,pin,pout)
    proposed={'status':status,'punch_in':pin,'punch_out':pout,**calc}
    fields=('status','punch_in','punch_out','work_hours','late_minutes','overtime_minutes','half_day')
    changes={}
    for k in fields:
        old=doc.get(k,False if k=='half_day' else 0 if k in ('late_minutes','overtime_minutes') else None)
        new=proposed[k]
        if old!=new: changes[k]={'from':old,'to':new}
    if not changes: raise HTTPException(422,'request does not change the record')
    entry={'at':now_utc(),'by':body.regularized_by,'reason':body.reason,'changes':changes}
    
    filter_doc={'emp_code':emp_code,'date':date,'_id':doc['_id']}
    for k in fields:
        if k in doc: filter_doc[k]=doc[k]
        else: filter_doc[k]={'$exists':False}
    updated=db.attendance_logs.find_one_and_update(filter_doc,{'$set':proposed,'$push':{'history':entry}},return_document=ReturnDocument.AFTER)
    if not updated: raise HTTPException(409,'record changed concurrently; retry')
    return serialize(updated)


def attendance_match(start,end,emp_code=None):
    q={'date':{'$gte':start,'$lte':end}}
    if emp_code:q['emp_code']=emp_code
    return q

def monthly_pipeline(emp_code,month):
    start,end=date_range_month(month)
    return start,end,[{'$match':attendance_match(start,end,emp_code)},{'$group':{'_id':None,'present_days':{'$sum':{'$cond':[{'$in':['$status',list(PRESENCE)]},{'$cond':[{'$eq':['$half_day',True]},0.5,1]},0]}},'leave_days':{'$sum':{'$cond':[{'$eq':['$status','LEAVE']},1,0]}},'late_count':{'$sum':{'$cond':[{'$gt':[{'$ifNull':['$late_minutes',0]},0]},1,0]}},'total_late_minutes':{'$sum':{'$ifNull':['$late_minutes',0]}},'total_overtime_minutes':{'$sum':{'$ifNull':['$overtime_minutes',0]}}}}]

@app.get('/analytics/employees/{emp_code}/monthly')
def employee_monthly(emp_code:str,month:str=Query(...,pattern=r'^\d{4}-(0[1-9]|1[0-2])$')):
    emp=db.employees.find_one({'emp_code':emp_code})
    if not emp: raise HTTPException(404,'employee not found')
    start,end,pipeline=monthly_pipeline(emp_code,month); result=list(db.attendance_logs.aggregate(pipeline)); agg=result[0] if result else {}
    y,m=map(int,month.split('-')); first=max(date(y,m,1),date.fromisoformat(emp['joined_on'])); last=date(y,m,calendar.monthrange(y,m)[1]); working=sum(1 for i in range((last-first).days+1) if (first+timedelta(days=i)).weekday()<5) if first<=last else 0
    present=half_up(agg.get('present_days',0),2)
    pct=half_up(present/working*100,2) if working else None
    return {'emp_code':emp_code,'month':month,'working_days':working,'present_days':present,'leave_days':agg.get('leave_days',0),'late_count':agg.get('late_count',0),'total_late_minutes':agg.get('total_late_minutes',0),'total_overtime_minutes':agg.get('total_overtime_minutes',0),'attendance_pct':pct}

@app.get('/analytics/departments/summary')
def department_summary(month:str=Query(...,pattern=r'^\d{4}-(0[1-9]|1[0-2])$'),department:Optional[str]=None):
    start,end=date_range_month(month)
    match={'$match':{'joined_on':{'$lte':end}}}
    if department is not None: match['$match']['department']=department
    pipeline=[match,{'$lookup':{'from':'attendance_logs','let':{'code':'$emp_code'},'pipeline':[{'$match':{'$expr':{'$and':[{'$eq':['$emp_code','$$code']},{'$gte':['$date',start]},{'$lte':['$date',end]}]}}}], 'as':'logs'}},{'$unwind':{'path':'$logs','preserveNullAndEmptyArrays':True}},{'$group':{'_id':'$department','headcount':{'$addToSet':'$emp_code'},'present_days':{'$sum':{'$cond':[{'$in':['$logs.status',list(PRESENCE)]},{'$cond':[{'$eq':['$logs.half_day',True]},0.5,1]},0]}},'hours_sum':{'$sum':{'$cond':[{'$and':[{'$in':['$logs.status',list(PRESENCE)]},{'$ne':[{'$ifNull':['$logs.work_hours',None]},None]}]},'$logs.work_hours',0]}},'hours_count':{'$sum':{'$cond':[{'$and':[{'$in':['$logs.status',list(PRESENCE)]},{'$ne':[{'$ifNull':['$logs.work_hours',None]},None]}]},1,0]}},'late_count':{'$sum':{'$cond':[{'$gt':[{'$ifNull':['$logs.late_minutes',0]},0]},1,0]}},'total_late_minutes':{'$sum':{'$ifNull':['$logs.late_minutes',0]}},'leave_count':{'$sum':{'$cond':[{'$eq':['$logs.status','LEAVE']},1,0]}},'on_duty_count':{'$sum':{'$cond':[{'$eq':['$logs.status','ON_DUTY']},1,0]}}}},{'$project':{'_id':0,'department':'$_id','headcount':{'$size':'$headcount'},'present_days':{'$round':['$present_days',2]},'avg_work_hours':{'$cond':[{'$gt':['$hours_count',0]},{'$round':[{'$divide':['$hours_sum','$hours_count']},2]},None]},'late_count':1,'total_late_minutes':1,'leave_count':1,'on_duty_count':1}},{'$sort':{'department':1}}]
    items=list(db.employees.aggregate(pipeline,allowDiskUse=True))
    return {'month':month,'items':items}

@app.get('/analytics/leaderboard/late')
def late_leaderboard(month:str=Query(...,pattern=r'^\d{4}-(0[1-9]|1[0-2])$'),limit:int=Query(10,ge=1,le=50),department:Optional[str]=None):
    start,end=date_range_month(month)
    pipeline=[{'$match':attendance_match(start,end)},{'$group':{'_id':'$emp_code','total_late_minutes':{'$sum':{'$ifNull':['$late_minutes',0]}},'late_count':{'$sum':{'$cond':[{'$gt':[{'$ifNull':['$late_minutes',0]},0]},1,0]}}}},{'$match':{'total_late_minutes':{'$gt':0}}},{'$lookup':{'from':'employees','localField':'_id','foreignField':'emp_code','as':'employee'}},{'$unwind':'$employee'}]
    if department is not None: pipeline.append({'$match':{'employee.department':department}})
    pipeline += [{'$setWindowFields':{'sortBy':{'total_late_minutes':-1},'output':{'rank':{'$rank':{}}}}},{'$match':{'rank':{'$lte':limit}}},{'$sort':{'total_late_minutes':-1,'_id':1}},{'$project':{'_id':0,'emp_code':'$_id','name':'$employee.name','department':'$employee.department','total_late_minutes':1,'late_count':1,'rank':1}}]
    return {'month':month,'items':list(db.attendance_logs.aggregate(pipeline,allowDiskUse=True))}

@app.get('/analytics/departments/{department}/trend')
def department_trend(department:str,from_date:str=Query(...,alias='from'),to_date:str=Query(...,alias='to')):
    try: iso_date(from_date); iso_date(to_date)
    except ValueError as e: raise HTTPException(422,str(e))
    first=date.fromisoformat(from_date); last=date.fromisoformat(to_date)
    if last<first: raise HTTPException(422,'to must be >= from')
    if (last-first).days+1>92: raise HTTPException(422,'range cannot exceed 92 days')
    if not db.employees.find_one({'department':department}): raise HTTPException(404,'department not found')
    
    pipeline=[{'$match':{'date':{'$gte':from_date,'$lte':to_date}}},{'$lookup':{'from':'employees','localField':'emp_code','foreignField':'emp_code','as':'employee'}},{'$unwind':'$employee'},{'$match':{'employee.department':department}},{'$group':{'_id':'$date','present_count':{'$sum':{'$cond':[{'$in':['$status',list(PRESENCE)]},{'$cond':[{'$eq':['$half_day',True]},0.5,1]},0]}},'late_count':{'$sum':{'$cond':[{'$gt':[{'$ifNull':['$late_minutes',0]},0]},1,0]}}}},{'$unionWith':{'coll':'attendance_logs','pipeline':[{'$documents':[{'_id':from_date,'present_count':0,'late_count':0}]}]}},{'$project':{'_id':0,'day':'$_id','present_count':1,'late_count':1}},{'$addFields':{'dayDate':{'$dateFromString':{'dateString':'$day','format':'%Y-%m-%d','timezone':'UTC'}}}},{'$densify':{'field':'dayDate','range':{'step':1,'unit':'day','bounds':[datetime.combine(first,time(),UTC),datetime.combine(last,time(),UTC)]}}},{'$addFields':{'day':{'$dateToString':{'date': '$dayDate','format':'%Y-%m-%d','timezone':'UTC'}},'present_count':{'$ifNull':['$present_count',0]},'late_count':{'$ifNull':['$late_count',0]},'is_working_day':{'$and':[{'$gte':[{'$dayOfWeek':'$dayDate'},2]},{'$lte':[{'$dayOfWeek':'$dayDate'},6]}]}}},{'$lookup':{'from':'employees','let':{'d':'$day'},'pipeline':[{'$match':{'$expr':{'$and':[{'$eq':['$department',department]},{'$lte':['$joined_on','$$d']}]}}},{'$count':'n'}],'as':'hc'}},{'$set':{'headcount':{'$ifNull':[{'$arrayElemAt':['$hc.n',0]},0]} }},{'$set':{'attendance_rate':{'$cond':[{'$and':['$is_working_day',{'$gt':['$headcount',0]}]},{'$round':[{'$divide':['$present_count','$headcount']},4]},None]}}},{'$setWindowFields':{'sortBy':{'dayDate':1},'output':{'moving_avg_7d':{'$avg':'$attendance_rate','window':{'documents':[-6,0]}}}}},{'$project':{'_id':0,'date':'$day','is_working_day':1,'headcount':1,'present_count':{'$round':['$present_count',2]},'late_count':1,'attendance_rate':1,'moving_avg_7d':{'$round':['$moving_avg_7d',4]}}},{'$sort':{'date':1}}]
    items=list(db.attendance_logs.aggregate(pipeline,allowDiskUse=True))
    return {'department':department,'items':items}

@app.get('/admin/explain/{endpoint}')
def explain_endpoint(endpoint:str,emp_code:Optional[str]=None,month:Optional[str]=None,department:Optional[str]=None,limit:int=Query(10,ge=1,le=50),date_from:Optional[str]=None,date_to:Optional[str]=None,status:Optional[str]=None,page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100),from_date:Optional[str]=Query(None,alias='from'),to_date:Optional[str]=Query(None,alias='to')):
    allowed={'attendance_list','employee_monthly','department_summary','late_leaderboard','department_trend'}
    if endpoint not in allowed: raise HTTPException(422,'unknown endpoint')
    try:
        if endpoint=='attendance_list':
            q={}
            if emp_code:q['emp_code']=emp_code
            if status:q['status']=status
            if date_from or date_to:
                q['date']={}
                if date_from:q['date']['$gte']=iso_date(date_from)
                if date_to:q['date']['$lte']=iso_date(date_to)
            if date_from and date_to and date_from>date_to: raise HTTPException(422,'date_from must be <= date_to')
            exp=db.command('explain',{'find':'attendance_logs','filter':q,'sort':{'date':-1,'emp_code':1},'skip':page_params(page,page_size),'limit':page_size},verbosity='executionStats'); coll='attendance_logs'
        else:
            if not month and endpoint in ('employee_monthly','department_summary','late_leaderboard'): raise HTTPException(422,'month is required')
            if endpoint=='employee_monthly':
                if not emp_code: raise HTTPException(422,'emp_code is required')
                _,_,pipe=monthly_pipeline(emp_code,month); exp=db.command('explain',{'aggregate':'attendance_logs','pipeline':pipe,'cursor':{}},verbosity='executionStats'); coll='attendance_logs'
            elif endpoint=='department_summary':
                start,end=date_range_month(month); exp=db.command('explain',{'aggregate':'employees','pipeline':[{'$match':{'joined_on':{'$lte':end},**({'department':department} if department is not None else {})}},{'$sort':{'department':1}}],'cursor':{}},verbosity='executionStats'); coll='employees'
            elif endpoint=='late_leaderboard':
                start,end=date_range_month(month); pipe=[{'$match':attendance_match(start,end)},{'$group':{'_id':'$emp_code','total_late_minutes':{'$sum':'$late_minutes'}}}]; exp=db.command('explain',{'aggregate':'attendance_logs','pipeline':pipe,'cursor':{}},verbosity='executionStats'); coll='attendance_logs'
            else:
                if not from_date or not to_date: raise HTTPException(422,'from and to are required')
                iso_date(from_date); iso_date(to_date)
                exp=db.command('explain',{'aggregate':'attendance_logs','pipeline':[{'$match':{'date':{'$gte':from_date,'$lte':to_date}}}],'cursor':{}},verbosity='executionStats'); coll='attendance_logs'
        return {'endpoint':endpoint,'collection':coll,'explain':json_util.loads(json_util.dumps(exp))}
    except HTTPException: raise
    except Exception as e: raise HTTPException(422,f'could not explain endpoint: {str(e)}')
