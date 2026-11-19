from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response

from app.container import Container, get_container
from app.schemas import PlanIn, PlanOut, PlanPatch, PlanSaveResult

router = APIRouter(prefix="/api/plans", tags=["plans"])


@router.get("", response_model=list[PlanOut])
def list_plans(c: Container = Depends(get_container)):
    return c.plans.list()


@router.post("", response_model=PlanSaveResult, status_code=201)
def create_plan(body: PlanIn, c: Container = Depends(get_container)):
    plan, conflicts = c.plans.create(body.model_dump())
    return {"plan": plan, "conflicts": conflicts}


@router.get("/{plan_id}", response_model=PlanOut)
def get_plan(plan_id: int, c: Container = Depends(get_container)):
    plan = c.plans.get(plan_id)
    if plan is None:
        raise HTTPException(404, "계획을 찾을 수 없습니다.")
    return plan


@router.put("/{plan_id}", response_model=PlanSaveResult)
def update_plan(plan_id: int, body: PlanPatch, c: Container = Depends(get_container)):
    current = c.plans.get(plan_id)
    if current is None:
        raise HTTPException(404, "계획을 찾을 수 없습니다.")
    patch = body.model_dump(exclude_unset=True)
    merged_end = patch.get("end_time", current["end_time"])
    merged_start = patch.get("start_time", current["start_time"])
    if merged_end and merged_end <= merged_start:
        raise HTTPException(422, "종료 시각은 시작 시각보다 늦어야 합니다.")
    result = c.plans.update(plan_id, patch)
    plan, conflicts = result  # type: ignore[misc]
    return {"plan": plan, "conflicts": conflicts}


@router.delete("/{plan_id}", status_code=204)
def delete_plan(plan_id: int, c: Container = Depends(get_container)):
    if not c.plans.delete(plan_id):
        raise HTTPException(404, "계획을 찾을 수 없습니다.")
    return Response(status_code=204)
