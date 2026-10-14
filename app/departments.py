"""根据记录创建人的组织架构确定业务部门。"""

import logging

logger = logging.getLogger(__name__)


def resolve_department(creator, rules: list[dict], feishu) -> str | None:
    """功能说明：从创建人直属部门向上查找规则部门。参数 creator：创建人字段值；rules：规则快照；feishu：通讯录客户端。返回值：唯一业务部门，未知或冲突时为空。"""
    people = creator if isinstance(creator, list) else [creator]
    if len(people) != 1 or not isinstance(people[0], dict) or not people[0].get("id"):
        logger.warning("创建人员部门未知：创建人缺失或无效")
        return None
    allowed = {name for rule in rules for name in rule["departments"]}
    matches = set()
    try:
        user = feishu.get_contact_user(people[0]["id"])
        departments = user.get("department_ids")
        if not departments:
            logger.warning("创建人员部门未知：人员部门字段为空或不可见")
            return None
        for department_id in departments:
            visited = set()
            while department_id and department_id != "0":
                if department_id in visited or len(visited) >= 32:
                    logger.warning("创建人员部门未知：组织层级循环或超出上限")
                    return None
                visited.add(department_id)
                department = feishu.get_contact_department(department_id)
                name = department.get("name")
                logger.debug("查询创建人部门层级：department=%s", name)
                if name in allowed:
                    matches.add(name)
                    break
                if "parent_department_id" not in department:
                    logger.warning("创建人员部门未知：上级部门字段不可见")
                    return None
                department_id = department["parent_department_id"]
            else:
                # 任一所属分支无法归属时，不依据其他分支擅自确定。
                logger.warning("创建人员部门未知：组织路径未命中规则部门")
                return None
    except Exception:
        logger.warning("创建人员部门未知：通讯录查询失败", exc_info=True)
        return None
    if len(matches) != 1:
        logger.warning("创建人员部门未知：匹配到多个业务部门")
        return None
    result = matches.pop()
    logger.debug("创建人部门识别完成：department=%s", result)
    return result
