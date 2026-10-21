"""A pupil's private revision becomes public only after its class approves it."""
import json

from fastapi.testclient import TestClient
from test_books import books, package, DRAFT


def student_client(books, actor='synthetic-pupil-one', group='class-one'):
    module, _ = books
    client = TestClient(module.app)
    client.headers.update({'Authorization':'Bearer synthetic-books-token',
                           'X-Llibres-Publisher':actor,'X-Llibres-Group':group})
    return client


def test_submission_review_return_update_and_withdraw(books):
    module, _ = books
    child=student_client(books);teacher=student_client(books,'synthetic-teacher-one')
    route='/api/internal/llibres/submissions/'+DRAFT
    assert child.get(route).json()['status']=='draft'
    first=child.post(route,content=package(editable=True),headers={'If-Match':'"0"'})
    assert first.status_code==200,first.text
    row=first.json();key=row['id'];assert row['status']=='pending' and not row['published']
    public=TestClient(module.app)
    assert public.get('/api/llibres/books').json()['items']==[]
    assert public.get('/api/llibres/public/'+key+'/index.html').status_code==404
    queue='/api/internal/llibres/reviews'
    assert teacher.post(queue,json={'groups':['other-class']}).json()['items']==[]
    assert len(teacher.post(queue,json={'groups':['class-one']}).json()['items'])==1
    files=queue+'/'+key+'/files/'
    preview=teacher.get(files+'index.html')
    assert preview.status_code==200
    assert 'allow-same-origin' in preview.headers['content-security-policy']
    assert 'allow-scripts' not in preview.headers['content-security-policy']
    assert preview.headers['cache-control']=='private, no-store'
    assert public.get(files+'cover.png').status_code==401
    outsider=student_client(books,'synthetic-other-teacher','other-class')
    assert outsider.get(files+'index.html').status_code==404
    review=queue+'/'+key
    assert outsider.post(review,json={'decision':'approve'},headers={'If-Match':'"1"'}).status_code==404
    approved=teacher.post(review,json={'decision':'approve'},headers={'If-Match':'"1"'})
    assert approved.status_code==200,approved.text
    approved=approved.json();assert approved['status']=='approved' and approved['approved_revision']==1
    assert approved['published'] and approved['revision']==2
    assert len(public.get('/api/llibres/books').json()['items'])==1
    original_url=approved['url']
    assert teacher.get(files+'index.html').status_code==404 # no redundant private copy after approval
    updated=package(title='Una versió pendent')
    assert child.post(route,content=updated,headers={'If-Match':'"2"'}).json()['status']=='pending'
    assert public.get('/api/llibres/books').json()['items'][0]['title']=='Un conte'
    with module.get_conn() as db:
        approval=db.execute('SELECT approved_by,approved_at,approved_revision FROM llibres_submissions WHERE id=?',(row['id'],)).fetchone()
        assert approval['approved_by']=='synthetic-teacher-one' and approval['approved_at'] and approval['approved_revision']==1

    assert teacher.post(review,json={'decision':'approve'},headers={'If-Match':'"1"'}).status_code==412
    assert teacher.post(review,json={'decision':'return'},headers={'If-Match':'"3"'}).status_code==422
    returned=teacher.post(review,json={'decision':'return','feedback':'Revisa els crèdits.'},headers={'If-Match':'"3"'}).json()
    assert returned['status']=='returned' and returned['feedback']=='Revisa els crèdits.'
    assert child.get(route).json()['feedback']=='Revisa els crèdits.'
    assert child.post(route,content=updated,headers={'If-Match':'"4"'}).json()['revision']==5
    again=teacher.post(review,json={'decision':'approve'},headers={'If-Match':'"5"'}).json()
    assert again['url']==original_url and again['status']=='approved'
    assert public.get('/api/llibres/books').json()['items'][0]['title']=='Una versió pendent'
    assert child.delete(route+'?publication=1',headers={'If-Match':'"6"'}).json()['status']=='cancelled'
    assert public.get('/api/llibres/books').json()['items']==[]
    assert public.get('/api/llibres/public/'+key+'/index.html').status_code==404


def test_ownership_revocation_quota_and_stale_decisions(books):
    module, _=books;child=student_client(books);teacher=student_client(books,'synthetic-teacher-one')
    route='/api/internal/llibres/submissions/'+DRAFT
    body=package();row=child.post(route,content=body,headers={'If-Match':'"0"'}).json();key=row['id']
    assert child.post(route,content=body,headers={'If-Match':'"0"'}).json()['revision']==1
    other=student_client(books,'synthetic-pupil-two')
    assert other.get(route).json()['status']=='draft'
    assert other.delete(route,headers={'If-Match':'"1"'}).status_code==404
    moved=student_client(books,group='class-two')
    assert moved.post(route,content=body,headers={'If-Match':'"1"'}).status_code==409
    module.book_publications.global_limit=1
    review='/api/internal/llibres/reviews/'+key
    assert teacher.post(review,json={'decision':'approve'},headers={'If-Match':'"1"'}).status_code==413
    assert child.get(route).json()['status']=='pending'
    assert teacher.get(review+'/files/index.html').status_code==200
    assert child.post(route,content=package(title='Changed'),headers={'If-Match':'"1"'}).status_code==413
    assert child.get(route).json()['revision']==1
    assert teacher.post(review,json={'decision':'withdraw'},headers={'If-Match':'"0"'}).status_code==412
    assert teacher.post(review,json={'decision':'withdraw'},headers={'If-Match':'"1"'}).json()['status']=='cancelled'
    assert teacher.get(review+'/files/index.html').status_code==404


def test_admin_can_clean_orphaned_requests_and_malformed_decisions_are_rejected(books):
    module,_=books;child=student_client(books,group='deleted-class')
    route='/api/internal/llibres/submissions/'+DRAFT
    row=child.post(route,content=package(),headers={'If-Match':'"0"'}).json()
    admin=student_client(books,'synthetic-admin-one',group='deleted-class')
    assert admin.post('/api/internal/llibres/reviews',json={'groups':[]}).json()['items']==[]
    assert len(admin.post('/api/internal/llibres/reviews',json={'groups':[], 'all_classes':True}).json()['items'])==1
    decision='/api/internal/llibres/reviews/'+row['id']
    assert admin.post(decision,content=b'bad json',headers={'If-Match':'"1"'}).status_code==422
    assert admin.post(decision,json=[],headers={'If-Match':'"1"'}).status_code==422
    assert admin.post(decision,json={'decision':'withdraw'},headers={'If-Match':'"1"'}).json()['status']=='cancelled'


def test_cancelling_new_submission_keeps_the_approved_publication(books):
    module,_=books;child=student_client(books);teacher=student_client(books,'synthetic-teacher-one')
    route='/api/internal/llibres/submissions/'+DRAFT
    row=child.post(route,content=package(),headers={'If-Match':'"0"'}).json()
    teacher.post('/api/internal/llibres/reviews/'+row['id'],json={'decision':'approve'},headers={'If-Match':'"1"'})
    assert child.post(route,content=package(title='Pending'),headers={'If-Match':'"2"'}).json()['status']=='pending'
    cancelled=child.delete(route,headers={'If-Match':'"3"'}).json()
    assert cancelled['status']=='cancelled' and cancelled['published']
    assert child.get('/api/llibres/books').json()['items'][0]['title']=='Un conte'
