"""Owner-issued client access and browser session continuation. No provider calls."""
import secrets
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import EmailStr, Field, SecretStr
from psycopg.types.json import Jsonb
from app.auth import actor_transaction, auth_event, current_user, issue_tokens, password_hash, require_admin, verify_password
from app.models import StrictModel, ShortText, RefreshRequest, TokenPair
from app.repository import lock_key, payload_hash
from app.session_cookie import COOKIE, require_same_origin, set_session_cookie, clear_session_cookie

router=APIRouter(tags=['Client access'])


class ClientCreate(StrictModel):
    brand_name: ShortText
    email: EmailStr
    full_name: ShortText
    temporary_password: Annotated[SecretStr,Field(min_length=12,max_length=128)] | None = None


class ClientView(StrictModel):
    id: UUID
    brand_id: UUID
    brand_name: str
    email: EmailStr
    full_name: str
    status: Literal['active','suspended']
    requires_password_change: bool
    onboarding_status: Literal['not_started','draft','submitted']
    created_at: datetime


class ClientCreated(StrictModel):
    account: ClientView
    temporary_password: str
    login_path: Literal['/login']='/login'


class PasswordChange(StrictModel):
    current_password: Annotated[SecretStr,Field(min_length=1,max_length=128)]
    new_password: Annotated[SecretStr,Field(min_length=12,max_length=128)]


class AccountAction(StrictModel):
    action: Literal['suspend','resume','reset_password']


class AccountResult(StrictModel):
    account: ClientView
    temporary_password: str | None = None


def directory(connection,organization_id):
    return connection.execute('SELECT mi.client_account_directory(%s) AS accounts',(organization_id,)).fetchone()['accounts']


@router.get('/v1/organizations/{organization_id}/clients',response_model=list[ClientView])
def list_clients(organization_id:UUID,connection=Depends(actor_transaction)):
    require_admin(connection,organization_id)
    return directory(connection,organization_id)


@router.post('/v1/organizations/{organization_id}/clients',response_model=ClientCreated,status_code=201)
def create_client(organization_id:UUID,body:ClientCreate,user=Depends(current_user),connection=Depends(actor_transaction)):
    require_admin(connection,organization_id)
    lock_key(connection,'client-email:'+str(body.email).lower())
    password=body.temporary_password.get_secret_value() if body.temporary_password else secrets.token_urlsafe(18)
    uid,bid,aid=uuid4(),uuid4(),uuid4()
    connection.execute('SELECT identity.create_client_user(%s,%s,%s,%s,%s)',(organization_id,uid,str(body.email).lower(),body.full_name,password_hash(password)))
    intake={'client_onboarding':True,'onboarding_pending':True}
    connection.execute('INSERT INTO mi.brand_workspaces(id,organization_id,name,website_url,intake,created_by,creation_key,creation_hash) VALUES(%s,%s,%s,NULL,%s,%s,%s,%s)',
                       (bid,organization_id,body.brand_name,Jsonb(intake),user['id'],'client:'+str(aid),payload_hash(intake)))
    connection.execute("INSERT INTO mi.organization_memberships(organization_id,user_id,role) VALUES(%s,%s,'client')",(organization_id,uid))
    connection.execute('INSERT INTO mi.brand_memberships(organization_id,brand_id,user_id,scopes) VALUES(%s,%s,%s,%s)',
                       (organization_id,bid,uid,['brand:read','onboarding:read','onboarding:write','published:read','approval:decide']))
    connection.execute('INSERT INTO mi.workflow_instances(id,organization_id,brand_id,created_by) VALUES(%s,%s,%s,%s)',(uuid4(),organization_id,bid,user['id']))
    connection.execute('INSERT INTO mi.client_accounts(id,organization_id,brand_id,user_id,created_by) VALUES(%s,%s,%s,%s,%s)',(aid,organization_id,bid,uid,user['id']))
    auth_event(connection,'client_access_created',True,user['id'],subject=str(body.email))
    account=next(a for a in directory(connection,organization_id) if a['id']==str(aid))
    return ClientCreated(account=account,temporary_password=password)


@router.post('/v1/organizations/{organization_id}/clients/{account_id}/access',response_model=AccountResult)
def manage_client(organization_id:UUID,account_id:UUID,body:AccountAction,user=Depends(current_user),connection=Depends(actor_transaction)):
    require_admin(connection,organization_id)
    row=connection.execute('SELECT * FROM mi.client_accounts WHERE id=%s AND organization_id=%s',(account_id,organization_id)).fetchone()
    if not row:raise HTTPException(404,'Client account not found')
    password=secrets.token_urlsafe(18) if body.action=='reset_password' else None
    connection.execute('SELECT identity.manage_client_user(%s,%s,%s,%s)',(organization_id,row['user_id'],body.action,password_hash(password) if password else None))
    auth_event(connection,'client_access_'+body.action,True,user['id'],subject=str(row['user_id']))
    account=next(a for a in directory(connection,organization_id) if a['id']==str(account_id))
    return AccountResult(account=account,temporary_password=password)


@router.post('/v1/auth/session',response_model=TokenPair,tags=['Authentication'])
def resume_session(request:Request,response:Response):
    require_same_origin(request)
    token=request.cookies.get(COOKIE)
    if not token:raise HTTPException(401,'Sign in to continue')
    from app.main import refresh
    return refresh(RefreshRequest(refresh_token=token),response)


@router.delete('/v1/auth/session',tags=['Authentication'])
def discard_session(request:Request,response:Response):
    require_same_origin(request)
    clear_session_cookie(response)
    return {'status':'cleared'}


@router.post('/v1/auth/password',response_model=TokenPair,tags=['Authentication'])
def change_password(body:PasswordChange,response:Response,user=Depends(current_user),connection=Depends(actor_transaction)):
    row=connection.execute('SELECT * FROM identity.login_lookup(%s)',(user['email'],)).fetchone()
    old,new=body.current_password.get_secret_value(),body.new_password.get_secret_value()
    if not row or not verify_password(row['password_hash'],old):raise HTTPException(400,'Current password is incorrect')
    if old==new:raise HTTPException(422,'Choose a different password')
    connection.execute('SELECT identity.change_own_password(%s)',(password_hash(new),))
    tokens=issue_tokens(connection,user['id'])
    auth_event(connection,'password_changed',True,user['id'])
    set_session_cookie(response,tokens)
    return tokens
