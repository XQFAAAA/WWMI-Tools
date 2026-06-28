import os
import re

from enum import Enum, auto
from dataclasses import dataclass


@dataclass
class Dispatch:
    ThreadGroupCountX: int
    ThreadGroupCountY: int
    ThreadGroupCountZ: int


@dataclass
class DrawIndexed:
    IndexCount: int
    StartIndexLocation: int
    BaseVertexLocation: int


class CallParameters(Enum):
    Dispatch = auto()
    DrawIndexed = auto()


class FrameDumpCall:
    def __init__(self, call_id):
        self.id = call_id
        self.parameters = {}
        # 该 call 中由 PSSetShaderResources 显式绑定的 PS 贴图槽位集合
        self.ps_texture_slots = set()
        self.patterns = {
            CallParameters.Dispatch: (
                re.compile(r'^Dispatch\(ThreadGroupCountX:(\d+), ThreadGroupCountY:(\d+), ThreadGroupCountZ:(\d+)\)'),
                lambda data: Dispatch(int(data[0]), int(data[1]), int(data[2]))
            ),
            CallParameters.DrawIndexed: (
                re.compile(r'^DrawIndexed\(IndexCount:(\d+), StartIndexLocation:(\d+), BaseVertexLocation:(\d+)\)'),
                lambda data: DrawIndexed(int(data[0]), int(data[1]), int(data[2]))
            ),
        }
        # PSSetShaderResources(StartSlot:N, NumViews:K, ...) 允许出现多次
        self.ps_sr_pattern = re.compile(r'PSSetShaderResources\(StartSlot:(\d+), NumViews:(\d+),')

    def import_data(self, raw_log_entry):
        raw_log_entry = ' '.join(raw_log_entry)
        for name, (pattern, decoder) in self.patterns.items():
            result = pattern.findall(raw_log_entry)
            if len(result) == 0:
                continue
            if len(result) != 1:
                raise ValueError(f'More than 1 data entries for pattern {pattern} in {raw_log_entry}')
            self.parameters[name] = decoder(result[0])

        # 解析 PSSetShaderResources，记录被显式绑定的 PS 贴图槽位
        # 每次 PSSetShaderResources(StartSlot:N, NumViews:K) 绑定连续 K 个槽位 [N, N+K-1]
        for start_str, count_str in self.ps_sr_pattern.findall(raw_log_entry):
            start = int(start_str)
            count = int(count_str)
            for slot in range(start, start + count):
                self.ps_texture_slots.add(slot)


class FrameDumpLog:
    def __init__(self, dump_path):
        self.path = os.path.join(dump_path, 'log.txt') # 拼接log.txt路径
        self.calls = {}
        self.parse_log()
        self.validate()

    def validate(self):
        pass

    def parse_log(self):
        with open(self.path, "r") as f:
            lines = f.readlines()

        # 按call_id分组
        grouped = []
        for line in lines:
            raw_call_id = line[0:6]
            if raw_call_id.isnumeric():
                grouped.append((raw_call_id, [line[7:]]))
            elif grouped: # 非数字开头，且不是第一行analyse_options
                grouped[-1][1].append(line.strip())

        call = None
        for raw_call_id, raw_log_entry in grouped:
            call_id = int(raw_call_id)
            # 首个或变更时，创建新的call
            if call is None or call_id != call.id:
                if call_id in self.calls:
                    raise ValueError(
                        f'Call id {raw_call_id} was already finished, '
                        f'current call id: {call_id}'
                    )
                call = FrameDumpCall(call_id)
                self.calls[raw_call_id] = call
            # 正则匹配
            call.import_data(raw_log_entry)
