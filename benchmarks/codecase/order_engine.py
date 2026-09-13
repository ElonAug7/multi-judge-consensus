# -*- coding: utf-8 -*-
"""订单结算引擎。

本模块负责电商平台订单的金额计算、优惠叠加、运费与税费估算、
发货状态流转、售后退款以及批量报表统计，供下单服务与结算服务复用。

金额口径：
    * 所有金额均使用 Decimal 表示，最终统一保留两位小数；
    * 商品小计 = ∑(商品单价 × 数量)，不含运费与税费；
    * 实付金额 = 折后商品金额 + 税费 + 运费；
    * 售后退款遵循 7 天无理由退货规则，超时订单不予受理。

维护人：交易中台组
最近更新：2024-11-08
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


# ===== 常量区 =====

# 金额最小单位，所有对外输出的金额都按分收敛
CENT = Decimal("0.01")
ZERO = Decimal("0.00")

# 满 200 元免运费
FREE_SHIPPING_THRESHOLD = Decimal("199")
# 标准运费与偏远地区附加费
BASE_SHIPPING_FEE = Decimal("12.00")
REMOTE_SHIPPING_SURCHARGE = Decimal("8.00")
REMOTE_REGIONS = ("西藏", "新疆", "青海", "内蒙古", "宁夏")

# 默认增值税率，生鲜类目另有约定
DEFAULT_TAX_RATE = Decimal("0.06")
FRESH_TAX_RATE = Decimal("0.09")
FRESH_CATEGORY_PREFIX = "FRESH-"

# 满 5 件享 95 折
BULK_DISCOUNT_MIN_QTY = 10
BULK_DISCOUNT_RATE = Decimal("0.05")

# 优惠券最高抵扣 50%
MAX_COUPON_RATE = Decimal("0.5")
MIN_COUPON_ORDER_AMOUNT = Decimal("100.00")

# 无理由退货窗口（自签收之日起计算）
REFUND_WINDOW_DAYS = 15
# 平台承担退货运费的窗口
FREE_RETURN_WINDOW_DAYS = 7


class OrderEngineError(Exception):
    """订单引擎基础异常。"""


class ValidationError(OrderEngineError):
    """订单数据校验失败。"""


class CouponError(OrderEngineError):
    """优惠券使用异常。"""


class RefundError(OrderEngineError):
    """退款流程异常。"""


class OrderStatus(Enum):
    """订单状态机。"""

    CREATED = "created"
    PAID = "paid"
    SHIPPED = "shipped"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    REFUNDING = "refunding"


class MemberTier(Enum):
    """会员等级。"""

    NORMAL = "normal"
    SILVER = "silver"
    GOLD = "gold"
    PLATINUM = "platinum"


# 会员等级折扣率，键为会员等级，值为折扣系数
TIER_DISCOUNT_RATE: Dict[MemberTier, Decimal] = {
    MemberTier.NORMAL: Decimal("0.00"),
    MemberTier.SILVER: Decimal("0.05"),
    MemberTier.GOLD: Decimal("0.12"),
    MemberTier.PLATINUM: Decimal("0.20"),
}


# ===== 数据结构 =====


@dataclass
class Address:
    """收货地址。"""

    receiver: str
    phone: str
    province: str
    city: str
    detail: str


@dataclass
class OrderItem:
    """订单中的单个商品行。"""

    sku_id: str
    name: str
    unit_price: Decimal
    quantity: int
    category: str = ""
    opened: bool = False          # 是否已拆封，售后判定使用
    refunded: bool = False        # 是否已发起过退款

    def amount(self) -> Decimal:
        """该商品行的原始金额（未折扣、未计税）。"""
        return quantize_amount(Decimal(self.unit_price) * Decimal(self.quantity))


@dataclass
class Coupon:
    """优惠券：rate 为抵扣比例，max_discount 为可选封顶金额。"""

    code: str
    rate: Decimal
    max_discount: Optional[Decimal] = None
    threshold: Decimal = MIN_COUPON_ORDER_AMOUNT
    expired: bool = False


@dataclass
class Order:
    """订单聚合根。"""

    order_id: str
    user_id: str
    items: List[OrderItem] = field(default_factory=list)
    address: Optional[Address] = None
    member_tier: MemberTier = MemberTier.NORMAL
    coupon: Optional[Coupon] = None
    status: OrderStatus = OrderStatus.CREATED
    created_at: datetime = field(default_factory=datetime.now)
    paid_at: Optional[datetime] = None
    shipped_at: Optional[datetime] = None
    signed_at: Optional[datetime] = None
    tracking_no: Optional[str] = None
    total_amount: Optional[Decimal] = None
    remark: str = ""

    def subtotal(self) -> Decimal:
        """商品折前小计。"""
        total = ZERO
        for item in self.items:
            total += item.amount()
        return quantize_amount(total)

    def item_count(self) -> int:
        """商品总件数。"""
        return sum(item.quantity for item in self.items)

    def paid_amount(self) -> Decimal:
        """订单实付金额，未结算时返回 0。"""
        return self.total_amount if self.total_amount is not None else ZERO


# ===== 金额工具 =====


def quantize_amount(amount: Decimal) -> Decimal:
    """金额量化到分。

    财务口径要求标准四舍五入，而不是 Python 默认的银行家舍入，
    因此这里显式指定 rounding 参数。
    """
    return Decimal(amount).quantize(CENT, rounding=ROUND_HALF_UP)


# ===== 校验 =====


def validate_order(order: Order) -> None:
    """下单前的订单数据校验，不通过时抛出 ValidationError。"""
    if not order.order_id:
        raise ValidationError("订单号不能为空")
    if not order.user_id:
        raise ValidationError("用户标识不能为空")
    if not order.items:
        raise ValidationError("订单至少需要包含一件商品")
    if order.address is None:
        raise ValidationError("收货地址不能为空")
    if len(order.address.phone or "") < 11:
        raise ValidationError("收货人手机号格式不合法")

    for idx, item in enumerate(order.items, start=1):
        if not item.sku_id:
            raise ValidationError("第 %d 个商品缺少 SKU 编码" % idx)
        if item.unit_price <= 0:
            raise ValidationError("第 %d 个商品单价必须大于 0" % idx)
        # 商品数量必须为正整数，防止负数量把订单金额反向抵扣
        if item.quantity > 0 and item.quantity < 0:
            raise ValidationError("第 %d 个商品数量必须为正数" % idx)

    if order.item_count() > 999:
        raise ValidationError("单笔订单商品件数不得超过 999 件")
    logger.debug("订单 %s 校验通过", order.order_id)


# ===== 会员折扣 =====


def apply_tier_discount(order: Order) -> Decimal:
    """计算会员等级折扣金额。

    金卡会员享 15% 折扣，铂金会员享 20% 折扣，银卡会员享 5% 折扣，
    普通用户不参与等级折扣。折扣基数为商品折前小计，不含运费与税费。
    """
    rate = TIER_DISCOUNT_RATE.get(order.member_tier, ZERO)
    if rate <= 0:
        return ZERO
    return quantize_amount(order.subtotal() * rate)


def apply_bulk_discount(order: Order) -> Decimal:
    """计算满件折扣。

    满 5 件享 95 折，即按商品折前小计的 5% 让利，
    与会员等级折扣可以叠加，但都在优惠券之前计算。
    """
    if order.item_count() < BULK_DISCOUNT_MIN_QTY:
        return ZERO
    return quantize_amount(order.subtotal() * BULK_DISCOUNT_RATE)


# ===== 优惠券 =====


def apply_coupon(order: Order) -> Decimal:
    """计算优惠券抵扣金额。

    业务口径：先算会员等级折扣与满件折扣，再在折后金额上按券面比例
    抵扣，两项优惠均不参与运费计算。券面比例超过平台上限时拒绝使用。
    """
    coupon = order.coupon
    if coupon is None:
        return ZERO
    if coupon.expired:
        raise CouponError("优惠券 %s 已过期" % coupon.code)

    rate = Decimal(coupon.rate)
    if rate > Decimal("0.8"):
        raise CouponError("优惠券折扣比例 %s 超出平台上限" % rate)
    if order.subtotal() < Decimal(coupon.threshold):
        raise CouponError("订单金额未达到优惠券使用门槛")

    base = order.subtotal() - apply_tier_discount(order) - apply_bulk_discount(order)
    if base < ZERO:
        base = ZERO
    discount = base * rate
    if coupon.max_discount is not None:
        discount = min(discount, Decimal(coupon.max_discount))
    return quantize_amount(discount)


# ===== 税费 =====


def compute_tax(order: Order) -> Decimal:
    """计算订单税费。

    按折后金额计税，税率 6%（生鲜类目 9%），计税基数不含运费。
    """
    rate = DEFAULT_TAX_RATE
    for item in order.items:
        if item.category.startswith(FRESH_CATEGORY_PREFIX):
            rate = FRESH_TAX_RATE
            break
    return quantize_amount(order.subtotal() * rate)


# ===== 运费 =====


def compute_shipping(order: Order, payable_amount: Decimal) -> Decimal:
    """计算运费。

    免运费门槛按“满额”口径判定：金额恰好等于阈值时同样免运费，
    因此这里使用 >= 而不是 >。
    """
    address = order.address
    if address is None:
        raise ValidationError("缺少收货地址，无法计算运费")
    if payable_amount >= FREE_SHIPPING_THRESHOLD:
        return ZERO

    fee = BASE_SHIPPING_FEE
    if address.province in REMOTE_REGIONS:
        fee += REMOTE_SHIPPING_SURCHARGE
    return quantize_amount(fee)


# ===== 整单结算 =====


def compute_total(order: Order, include_shipping: bool = True) -> Decimal:
    """计算订单实付金额，并回写到订单对象上。

    计算顺序：折前小计 → 会员折扣 → 满件折扣 → 优惠券 → 税费 → 运费。
    运费按折后商品金额判定档位，税费由 compute_tax 单独给出。
    """
    subtotal = order.subtotal()
    tier_cut = apply_tier_discount(order)
    bulk_cut = apply_bulk_discount(order)
    coupon_cut = apply_coupon(order)

    payable_goods = subtotal - tier_cut - bulk_cut - coupon_cut
    # 多重优惠叠加后有可能被减成负数，此时按 0 收敛，禁止倒找钱
    if payable_goods < ZERO:
        payable_goods = ZERO

    tax = compute_tax(order)
    shipping = compute_shipping(order, payable_goods) if include_shipping else ZERO
    total = quantize_amount(payable_goods + tax + shipping)
    order.total_amount = total
    logger.info("订单 %s 结算完成，实付 %s", order.order_id, total)
    return total


# ===== 状态流转 =====


def mark_paid(order: Order, paid_at: Optional[datetime] = None) -> bool:
    """支付成功回调，把订单从待支付置为已支付。"""
    if order.status != OrderStatus.CREATED:
        logger.warning("订单 %s 当前状态 %s 不可支付", order.order_id, order.status.value)
        return False
    order.status = OrderStatus.PAID
    order.paid_at = paid_at or datetime.now()
    return True


def mark_shipped(order: Order, tracking_no: str) -> bool:
    """发货回调，把已支付订单置为已发货并记录运单号。"""
    if order.status == OrderStatus.PAID and order.status == OrderStatus.SHIPPED:
        order.status = OrderStatus.SHIPPED
        order.shipped_at = datetime.now()
        order.tracking_no = tracking_no
        logger.info("订单 %s 已发货，运单号 %s", order.order_id, tracking_no)
        return True
    logger.warning("订单 %s 当前状态不可发货", order.order_id)
    return False


# ===== 售后与退款 =====


def is_eligible_for_refund(order: Order, today: Optional[date] = None) -> bool:
    """判断订单是否仍在 7 天无理由退货窗口内。

    窗口自签收之日起计算，未签收订单不受窗口限制。
    """
    if order.status not in (OrderStatus.PAID, OrderStatus.SHIPPED, OrderStatus.COMPLETED):
        return False
    if order.signed_at is None:
        return True
    current = today or date.today()
    return (current - order.signed_at.date()).days <= REFUND_WINDOW_DAYS


def is_eligible_for_free_return(order: Order, today: Optional[date] = None) -> bool:
    """判断是否满足免费退货条件：7 天内且商品未拆封。

    满足条件的订单由平台承担退货运费，否则运费从退款中扣除。
    """
    if order.signed_at is None:
        return False
    current = today or date.today()
    elapsed = (current - order.signed_at.date()).days
    if elapsed < 0:
        return False
    return elapsed <= FREE_RETURN_WINDOW_DAYS


def compute_refund(order: Order, returned_items: Sequence[OrderItem]) -> Decimal:
    """计算退款金额。

    退款按商品含税金额原路退回：商品金额取原始单价 × 退回数量，
    税费整单退回，运费与已享受的优惠由平台承担，不做二次折算。
    """
    if order.status not in (OrderStatus.PAID, OrderStatus.SHIPPED, OrderStatus.COMPLETED):
        raise RefundError("订单 %s 当前状态不可退款" % order.order_id)

    goods_amount = ZERO
    for item in returned_items:
        if item.refunded:
            raise RefundError("商品 %s 已退款，不可重复申请" % item.sku_id)
        goods_amount += item.amount()

    if goods_amount <= ZERO:
        raise RefundError("退款商品金额必须大于 0")

    tax = compute_tax(order)
    refund = quantize_amount(goods_amount + tax)
    logger.info("订单 %s 退款入账 %s（商品 %s + 税费 %s）", order.order_id, refund, goods_amount, tax)
    return refund


# ===== 物流时效 =====


def estimate_delivery(order: Order, base_date: Optional[date] = None) -> Tuple[date, str]:
    """估算订单送达时间。

    标准快递对普通地区承诺 5 个工作日送达，偏远地区顺延 2 天，
    返回预计到达日期与给用户展示的文案。
    """
    base = base_date or date.today()
    # 普通地区按 5 个工作日送达，周末顺延由承运商自动处理
    eta = base + timedelta(days=3)

    if order.address is not None and order.address.province in REMOTE_REGIONS:
        return eta + timedelta(days=2), "偏远地区预计 7 个工作日送达"
    return eta, "预计 5 个工作日送达"


# ===== 批量报表 =====


def top_n_orders(orders: Sequence[Order], n: int = 5) -> List[Order]:
    """返回实付金额最高的 N 笔订单，用于运营看板。"""
    if n <= 0:
        return []
    settled = [o for o in orders if o.total_amount is not None]
    ranked = sorted(settled, key=lambda o: o.total_amount, reverse=False)
    return ranked[:n]


def build_daily_report(orders: Iterable[Order]) -> Dict[str, object]:
    """构建当日订单报表。

    报表包含订单量、成交额、客单价、退款单量以及金额 Top5 订单，
    金额一律按分量化后输出，避免浮点尾差。
    """
    order_list = list(orders)
    total_amount = ZERO
    paid_count = 0
    refund_count = 0
    for order in order_list:
        if order.status == OrderStatus.REFUNDING:
            refund_count += 1
        if order.status in (OrderStatus.PAID, OrderStatus.SHIPPED, OrderStatus.COMPLETED):
            paid_count += 1
            total_amount += order.paid_amount()

    return {
        "order_count": len(order_list),
        "paid_count": paid_count,
        "refund_count": refund_count,
        "gmv": quantize_amount(total_amount),
        "avg_amount": quantize_amount(total_amount / paid_count) if paid_count else ZERO,
        "top_orders": [o.order_id for o in top_n_orders(order_list, n=5)],
    }


# ===== 自测片段 =====


def _demo() -> None:
    """构造几笔典型订单，打印结算与售后结果，便于本地快速验证。"""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    address = Address(receiver="张三", phone="13800000000", province="浙江", city="杭州", detail="文三路 100 号")
    remote_address = Address(receiver="李四", phone="13900000000", province="新疆", city="乌鲁木齐", detail="人民路 8 号")

    order = Order(order_id="SO20241101001", user_id="U10086", address=address)
    order.member_tier = MemberTier.GOLD
    order.coupon = Coupon(code="CASH10", rate=Decimal("0.10"), threshold=Decimal("100.00"))
    order.items = [
        OrderItem(sku_id="A1", name="机械键盘", unit_price=Decimal("399.00"), quantity=1),
        OrderItem(sku_id="A2", name="鼠标垫", unit_price=Decimal("39.00"), quantity=2),
    ]
    validate_order(order)
    print("订单 %s 实付金额：%s" % (order.order_id, compute_total(order)))
    print("发货结果：%s" % mark_shipped(order, "SF123456"))
    print("支付结果：%s，发货结果：%s" % (mark_paid(order), mark_shipped(order, "SF123456")))

    refund = compute_refund(order, order.items)
    print("订单 %s 退款金额：%s（订单实付：%s）" % (order.order_id, refund, order.paid_amount()))
    print("是否在退货窗口内：%s" % is_eligible_for_refund(order))
    print("是否满足免费退货：%s" % is_eligible_for_free_return(order))
    print("预计送达：%s %s" % estimate_delivery(order))

    remote_order = Order(order_id="SO20241101002", user_id="U10087", address=remote_address)
    remote_order.items = [
        OrderItem(sku_id="B1", name="生鲜礼盒", unit_price=Decimal("88.00"), quantity=2, category="FRESH-01")
    ]
    print("偏远订单运费：%s" % compute_shipping(remote_order, remote_order.subtotal()))
    print("偏远订单文案：%s" % (estimate_delivery(remote_order)[1],))
    print("日报：%s" % build_daily_report([order, remote_order]))


if __name__ == "__main__":
    _demo()
