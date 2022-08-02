# Copyright (C) 2017 Mandiant, Inc. All Rights Reserved.

from typing import List
from itertools import chain
from collections import namedtuple

import tqdm
import viv_utils
import envi.archs.i386
import envi.archs.amd64
import viv_utils.emulator_drivers

import floss.utils
import floss.strings
from floss.const import MAX_STRING_LENGTH
from floss.results import StackString

logger = floss.logging.getLogger(__name__)
MAX_STACK_SIZE = 0x10000

MIN_NUMBER_OF_MOVS = 5

CallContext = namedtuple(
    "CallContext",
    [
        "pc",  # the current program counter, type: int
        "sp",  # the current stack counter, type: int
        "init_sp",  # the initial stack counter at start of function, type: int
        "stack_memory",  # the active stack frame contents, type: str
    ],
)


class StackstringContextMonitor(viv_utils.emulator_drivers.Monitor):
    """
    Observes emulation and extracts the active stack frame contents:
      - at each function call in a function, and
      - based on heuristics looking for mov instructions to a hardcoded buffer.
    """

    def __init__(self, vw, init_sp, bb_ends):
        viv_utils.emulator_drivers.Monitor.__init__(self, vw)
        self.ctxs: List[CallContext] = []

        self._init_sp = init_sp
        # index of VAs of the last instruction of all basic blocks
        self._bb_ends = bb_ends
        # count of stack mov instructions in current basic block.
        # not guaranteed to grow greater than MIN_NUMBER_OF_MOVS.
        self._mov_count = 0

    # overrides emulator_drivers.Monitor
    def apicall(self, emu, op, pc, api, argv):
        self.extract_context(emu, op)

    def extract_context(self, emu, op):
        """
        Extract only the bytes on the stack between the base pointer
         (specifically, stack pointer at function entry),
        and stack pointer.
        """
        try:
            ctx = self.get_call_context(emu, op)
        except ValueError as e:
            logger.debug(str(e))
            return
        self.ctxs.append(ctx)

    def get_call_context(self, emu, op):
        stack_top = emu.getStackCounter()
        stack_bottom = self._init_sp
        stack_size = stack_bottom - stack_top
        if stack_size > MAX_STACK_SIZE:
            raise ValueError("stack size too big: 0x%x" % stack_size)
        stack_buf = emu.readMemory(stack_top, stack_size)
        return CallContext(op.va, stack_top, stack_bottom, stack_buf)

    # overrides emulator_drivers.Monitor
    def posthook(self, emu, op, endpc):
        self.check_mov_heuristics(emu, op, endpc)

    def check_mov_heuristics(self, emu, op, endpc):
        """
        Extract contexts at end of a basic block (bb) if bb contains enough movs to a harcoded buffer.
        """
        # TODO check number of written bytes?
        # count movs, shortcut if this basic block has enough writes to trigger context extraction already
        if self._mov_count < MIN_NUMBER_OF_MOVS and self.is_stack_mov(op):
            self._mov_count += 1

        if endpc in self._bb_ends:
            if self._mov_count >= MIN_NUMBER_OF_MOVS:
                self.extract_context(emu, op)
            # reset counter at end of basic block
            self._mov_count = 0

    def is_stack_mov(self, op):
        if not op.mnem.startswith("mov"):
            return False

        opnds = op.getOperands()
        return (
            isinstance(
                opnds[0],
                (
                    envi.archs.i386.disasm.i386SibOper,
                    envi.archs.i386.disasm.i386RegMemOper,
                ),
            )
            if opnds
            else True
        )


def extract_call_contexts(vw, fva, bb_ends):
    emu = floss.utils.make_emulator(vw)
    monitor = StackstringContextMonitor(vw, emu.getStackCounter(), bb_ends)
    driver = viv_utils.emulator_drivers.FunctionRunnerEmulatorDriver(emu)
    driver.add_monitor(monitor)
    driver.runFunction(fva, maxhit=1, maxrep=0x100, func_only=True)
    return monitor.ctxs


def getPointerSize(vw):
    if isinstance(vw.arch, envi.archs.amd64.Amd64Module):
        return 8
    elif isinstance(vw.arch, envi.archs.i386.i386Module):
        return 4
    else:
        raise NotImplementedError(
            f"unexpected architecture: {vw.arch.__class__.__name__}"
        )


def get_basic_block_ends(vw):
    """
    Return the set of VAs that are the last instructions of basic blocks.
    """
    index = set([])
    for funcva in vw.getFunctions():
        f = viv_utils.Function(vw, funcva)
        for bb in f.basic_blocks:
            if len(bb.instructions) == 0:
                continue
            index.add(bb.instructions[-1].va)
    return index


def extract_stackstrings(vw, selected_functions, min_length, no_filter=False, quiet=False):
    """
    Extracts the stackstrings from functions in the given workspace.

    :param vw: The vivisect workspace from which to extract stackstrings.
    :param selected_functions: list of selected functions
    :param min_length: minimum string length
    :param no_filter: do not filter deobfuscated stackstrings
    :param quiet: do NOT show progress bar
    :rtype: Generator[StackString]
    """
    # TODO add test sample(s) and tests
    logger.debug("extracting stackstrings from %d functions", len(selected_functions))
    bb_ends = get_basic_block_ends(vw)

    pbar = tqdm.tqdm
    if quiet:
        # do not use tqdm to avoid unnecessary side effects when caller intends
        # to disable progress completely
        pbar = lambda s, *args, **kwargs: s

    pb = pbar(selected_functions, desc="extracting stackstrings", unit=" functions")
    with tqdm.contrib.logging.logging_redirect_tqdm(), floss.utils.redirecting_print_to_tqdm():
        for fva in pb:
            logger.debug("extracting stackstrings from function: 0x%x", fva)
            seen = set([])
            for ctx in extract_call_contexts(vw, fva, bb_ends):
                logger.trace(
                    "extracting stackstrings at checkpoint: 0x%x stacksize: 0x%x", ctx.pc, ctx.init_sp - ctx.sp
                )
                for s in chain(
                    floss.strings.extract_ascii_strings(ctx.stack_memory),
                    floss.strings.extract_unicode_strings(ctx.stack_memory),
                ):
                    if len(s.string) > MAX_STRING_LENGTH:
                        continue

                    if no_filter:
                        decoded_string = s.string
                    elif not floss.utils.is_fp_string(s.string):
                        decoded_string = floss.utils.strip_string(s.string)
                    else:
                        continue

                    if decoded_string not in seen and len(decoded_string) >= min_length:
                        frame_offset = (ctx.init_sp - ctx.sp) - s.offset - getPointerSize(vw)
                        ss = StackString(
                            fva, decoded_string, s.encoding, ctx.pc, ctx.sp, ctx.init_sp, s.offset, frame_offset
                        )
                        # TODO option/format to log quiet and regular, this is verbose output here currently
                        logger.info("%s [%s] in 0x%x at frame offset 0x%x", ss.string, s.encoding, fva, ss.frame_offset)
                        yield ss
                        seen.add(decoded_string)
