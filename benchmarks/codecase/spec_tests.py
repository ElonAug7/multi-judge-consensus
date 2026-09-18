#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""order_engine 业务规格测试（spec tests）—— 按 docstring 声明的「正确业务口径」写断言。

用途：验证「上下文注入」假设——把规格/测试/运行失败喂给审阅者，能否突破纯读代码的召回天花板。
每个检查对应 GROUND_TRUTH 里的一个缺陷 D1..D12，失败信息即「业务上下文」。

仅用于基准实验，不修改被测代码。
"""
import sys
import os
from datetime import date, timedelta
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from order_engine import (
    Address, Order, OrderItem, Coupon, MemberTier, OrderStatus,
    apply_tier_discount, apply_bulk_discount, apply_coupon, compute_tax,
    compute_shipping, compute_total, validate_order, mark_paid, mark_shipped,
    is_eligible_for_refund, is_eligible_for_free_return, compute_refund,
    estimate_delivery, top_n_orders, ValidationError, CouponError,
)


def make_order(unit_price="100.00", qty=1, tier=MemberTier.NORMAL, coupon_rate=None):
    o = Order(order_id="SO1", user_id="U1", address=Address("张", "13800000000", "浙江", "杭州", "x"))
    o.member_tier = tier
    o.items = [OrderItem(sku_id="A1", name="商品", unit_price=Decimal(unit_price), quantity=qty)]
    if coupon_rate is not None:
        o.coupon = Coupon(code="C1", rate=Decimal(coupon_rate), threshold=Decimal("0.00"))
    return o


CHECKS = []


def check(did, desc, fn):
    CHECKS.append((did, desc, fn))


# D1 金卡 15% vs 0.12
check("D1", "apply_tier_discount：docstring 称金卡享 15% 折扣", lambda: (
    apply_tier_discount(make_order("100.00", 1, MemberTier.GOLD)) == Decimal("15.00")))

# D2 免运费门槛 199 vs 200
check("D2", "compute_shipping：满 200 元才免运费（199 元应收运费）", lambda: (
    compute_shipping(make_order("199.00"), Decimal("199.00")) != Decimal("0.00")))

# D3 数量校验不可达
def _d3():
    o = make_order("100.00", -1)
    try:
        validate_order(o)
        return False  # 应该抛 ValidationError
    except ValidationError:
        return True
check("D3", "validate_order：负数量应抛 ValidationError", _d3)

# D4 计税基数折前 vs 折后
check("D4", "compute_tax：按折后金额计税（金卡折扣后）", lambda: (
    compute_tax(make_order("100.00", 1, MemberTier.GOLD)) == Decimal("5.10")))  # (100-15)*6%

# D5 退货窗口 7 vs 15
def _d5():
    o = make_order("100.00")
    o.status = OrderStatus.COMPLETED
    o.signed_at = o.created_at - timedelta(days=10)
    return not is_eligible_for_refund(o)  # 10 天 > 7 天，不应在窗口内
check("D5", "is_eligible_for_refund：7 天无理由（签收 10 天后不应受理）", _d5)

# D6 mark_shipped 不可达
def _d6():
    o = make_order("100.00")
    o.status = OrderStatus.PAID
    return mark_shipped(o, "SF1") is True  # 已支付应可发货
check("D6", "mark_shipped：已支付订单应可发货", _d6)

# D7 满件折扣 5 vs 10
check("D7", "apply_bulk_discount：满 5 件享 95 折（5 件应有折扣）", lambda: (
    apply_bulk_discount(make_order("100.00", 5)) != Decimal("0.00")))

# D8 退款 > 实付
def _d8():
    o = make_order("100.00", 1, MemberTier.GOLD, coupon_rate="0.50")
    o.status = OrderStatus.PAID
    paid = compute_total(o)
    refund = compute_refund(o, o.items)
    return refund <= paid  # 退款不得超过实付
check("D8", "compute_refund：退款不得超过实付金额", _d8)

# D9 优惠券上限 0.5 vs 0.8
def _d9():
    o = make_order("100.00", 1, coupon_rate="0.60")
    try:
        apply_coupon(o)
        return False  # 0.6 > 0.5 上限，应抛 CouponError
    except CouponError:
        return True
check("D9", "apply_coupon：券面比例超 50% 上限应拒绝", _d9)

# D10 top_n 排序反向
def _d10():
    a = make_order("100.00"); a.total_amount = Decimal("50.00")
    b = make_order("100.00"); b.total_amount = Decimal("500.00")
    top = top_n_orders([a, b], n=1)
    return top and top[0].total_amount == Decimal("500.00")  # 应取最高
check("D10", "top_n_orders：应返回金额最高（而非最低）", _d10)

# D11 免费退货未校验拆封
def _d11():
    o = make_order("100.00")
    o.signed_at = o.created_at - timedelta(days=2)
    o.items[0].opened = True  # 已拆封
    return not is_eligible_for_free_return(o)  # 未拆封才免费退
check("D11", "is_eligible_for_free_return：已拆封商品不满足免费退货", _d11)

# D12 送达时效 3 vs 5 工作日
def _d12():
    o = make_order("100.00")
    eta, _ = estimate_delivery(o, base_date=date(2024, 1, 1))
    return (eta - date(2024, 1, 1)).days >= 5  # 普通地区承诺 5 个工作日
check("D12", "estimate_delivery：普通地区应约 5 个工作日送达", _d12)


def main():
    fails = []
    for did, desc, fn in CHECKS:
        try:
            ok = bool(fn())
        except Exception as e:
            ok = False
            detail = "异常: %s" % e
        else:
            detail = ""
        status = "PASS" if ok else "FAIL"
        if not ok:
            fails.append((did, desc, detail))
        print(f"[{status}] {did} — {desc} {detail}")
    print("\n%d/%d 通过" % (len(CHECKS) - len(fails), len(CHECKS)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
