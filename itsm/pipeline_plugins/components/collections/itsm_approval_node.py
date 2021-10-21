# -*- coding: utf-8 -*-
"""
Tencent is pleased to support the open source community by making BK-ITSM 蓝鲸流程服务 available.

Copyright (C) 2021 THL A29 Limited, a Tencent company.  All rights reserved.

BK-ITSM 蓝鲸流程服务 is licensed under the MIT License.

License for BK-ITSM 蓝鲸流程服务:
--------------------------------------------------------------------
Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
documentation files (the "Software"), to deal in the Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software,
and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial
portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT
LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN
NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
"""

import logging
from django.core.cache import cache
from itsm.component.constants import NODE_APPROVE_RESULT, PROCESS_COUNT, APPROVE_RESULT
from itsm.ticket.models import Ticket, Status, TicketField
from pipeline.component_framework.component import Component

from .itsm_sign import ItsmSignService
from .tasks import auto_approve

logger = logging.getLogger("celery")


class ItsmApprovalService(ItsmSignService):
    __need_schedule__ = True
    __multi_callback_enabled__ = True

    def execute(self, data, parent_data):
        """进入审批节点的准备"""

        if super(ItsmSignService, self).execute(data, parent_data):
            return True

        logger.info("itsm_sign execute: data={}, parent_data={}".format(data.inputs, parent_data.inputs))
        ticket_id = parent_data.inputs.ticket_id
        state_id = data.inputs.state_id
        ticket = Ticket.objects.get(id=ticket_id)

        variables, _, code_key = ticket.do_before_enter_sign_state(state_id, by_flow=self.by_flow)
        is_multi = ticket.flow.get_state(state_id)["is_multi"]
        user_count = str(self.get_user_count(ticket_id, state_id)) if is_multi else "1"
        ticket.create_moa_ticket(state_id)

        finish_condition = self.get_finish_condition(user_count)
        # Set outputs to data
        data.set_outputs("variables", variables)
        data.set_outputs("finish_condition", finish_condition)
        data.set_outputs("code_key", code_key)
        node_status = Status.objects.get(ticket_id=ticket.id, state_id=state_id)
        
        if self.is_skip_approve(ticket, state_id, node_status):
            node_status.processors_type = "PERSON"
            node_status.processors = "system"
            node_status.save()
            msg = "检测到当前处理人为空，系统自动过单"
            callback_data = {
                "fields": self.get_approve_fields(ticket_id, state_id, msg),
                "ticket_id": ticket_id,
                'source': 'SYS',
                'operator': "system",
                "state_id": state_id
            }
            logger.info(
                "检测到当前单据处理人为空，系统即将准备自动过单, ticket_id={}, state_id={}, callback_data={}".format(
                    ticket.id, state_id, callback_data))
            activity_id = ticket.activity_for_state(state_id)
            auto_approve.apply_async((node_status.id, "system", activity_id, callback_data),
                                     countdown=20)  # 20秒之后自动回调
            return True

        if self.is_auto_approve(ticket, node_status):
            msg = "检测到当前处理人包含提单人，系统自动过单"
            callback_data = {
                "fields": self.get_approve_fields(ticket_id, state_id, msg),
                "ticket_id": ticket_id,
                'source': 'SYS',
                'operator': ticket.creator,
                "state_id": state_id
            }
            logger.info(
                "检测到当前单据开启了自动过单，即将准备自动过单, ticket_id={}, state_id={}, callback_data={}".format(
                    ticket.id, state_id, callback_data))
            activity_id = ticket.activity_for_state(state_id)
            auto_approve.apply_async((node_status.id, ticket.creator, activity_id, callback_data),
                                     countdown=20)  # 20秒之后自动回调

        return True

    def is_skip_approve(self, ticket, state_id, node_status):
        processors = node_status.get_processors()
        state = ticket.state(state_id)
        if state.get("is_allow_skip", False) and len(processors) == 0:
            return True
        return False

    def is_auto_approve(self, ticket, node_status):
        processors = node_status.get_processors()
        if ticket.flow.is_auto_approve and ticket.creator in processors:
            return True
        return False

    def get_approve_fields(self, ticket_id, state_id, msg):
        node_fields = TicketField.objects.filter(state_id=state_id, ticket_id=ticket_id)
        fields = []
        remarked = False
        for field in node_fields:
            if field.meta.get("code") == APPROVE_RESULT:
                fields.append(
                    {
                        "id": field.id,
                        "key": field.key,
                        "type": field.type,
                        "choice": field.choice,
                        "value": 'true',
                    }
                )
            else:
                if not remarked:
                    fields.append(
                        {
                            "id": field.id,
                            "key": field.key,
                            "type": field.type,
                            "choice": field.choice,
                            "value": msg,
                        }
                    )
                    remarked = True

        return fields

    @staticmethod
    def get_finish_condition(user_count):
        finish_condition = {
            "key": PROCESS_COUNT,
            "condition": ">=",
            "value": user_count,
        }
        return finish_condition

    @staticmethod
    def get_user_count(ticket_id, state_id):
        node_status = Status.objects.get(ticket_id=ticket_id, state_id=state_id)
        user_list = node_status.get_user_list()
        return len(user_list)

    @staticmethod
    def get_key_value(node_status, ticket, code_key):
        key_value = {}
        reject_count = node_status.sign_reject_count()
        if reject_count > 0:
            key_value[code_key[NODE_APPROVE_RESULT]] = "false"
        return key_value

    @staticmethod
    def task_is_finished(node_status, finish_condition, key_value, code_key):
        if key_value.get(code_key[NODE_APPROVE_RESULT]) == "false":
            return True
        process_count = node_status.sign_process_count()
        is_finished = True if process_count >= int(finish_condition["value"]) else False
        if is_finished:
            key_value[code_key[NODE_APPROVE_RESULT]] = "true"
        return is_finished

    @staticmethod
    def do_before_exit(ticket, state_id, operator):
        ticket.do_before_exit_sign_state(state_id)
        ticket.close_moa_ticket(state_id, operator)

    def final_execute(self, node_status, operator):
        super(ItsmApprovalService, self).final_execute(node_status, operator)
        cache.delete("approval_status_{}_{}_{}".format(operator, node_status.ticket_id, node_status.state_id))

    def outputs_format(self):
        return []


class ItsmApprovalComponent(Component):
    name = "审批节点原子"
    code = "itsm_approval_node"
    bound_service = ItsmApprovalService
