from __future__ import annotations

import membership
from auth import (
    authenticate,
    create_access_token,
    get_current_user,
    register_user,
)
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    email: str = Field(min_length=5, max_length=200)
    password: str = Field(min_length=8, max_length=128)


class LoginRequest(BaseModel):
    account: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=128)


@router.post("/register")
def register(payload: RegisterRequest):
    try:
        user = register_user(payload.email, payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    token = create_access_token(user["id"])
    return {"token": token, "user": user}


@router.post("/login")
def login(payload: LoginRequest):
    try:
        user = authenticate(payload.account, payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    token = create_access_token(user["id"])
    return {"token": token, "user": user}


@router.get("/me")
def me(user: dict = Depends(get_current_user)):
    member_status = membership.get_member_status(user_id=user["id"])
    return {"user": user, "membership": member_status}
