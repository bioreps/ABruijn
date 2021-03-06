#(c) 2016 by Authors
#This file is a part of ABruijn program.
#Released under the BSD license (see LICENSE file)

"""
Runs mapper (Minimap2(default) or GraphMap) and parses its output
"""

import os
import re
import sys
from collections import namedtuple, defaultdict
import subprocess
import logging
import multiprocessing
import ctypes

import abruijn.fasta_parser as fp
from abruijn.utils import which
import abruijn.config as config


logger = logging.getLogger()
MINIMAP_BIN = "abruijn-minimap2"
GRAPHMAP_BIN = "abruijn-graphmap"

Alignment = namedtuple("Alignment", ["qry_id", "trg_id", "qry_start", "qry_end",
                                     "qry_sign", "qry_len", "trg_start",
                                     "trg_end", "trg_sign", "trg_len",
                                     "qry_seq", "trg_seq", "err_rate"])

ContigInfo = namedtuple("ContigInfo", ["id", "length", "type"])


class AlignmentException(Exception):
    pass


class SynchronizedSamReader(object):
    """
    Parsing SAM file in multiple threads
    """
    def __init__(self, sam_alignment, reference_fasta, min_aln_length):
        #will not be changed during exceution
        self.aln_path = sam_alignment
        self.ref_fasta = reference_fasta
        self.change_strand = True
        self.min_aln_length = min_aln_length

        #will be shared between processes
        self.lock = multiprocessing.Lock()
        self.eof = multiprocessing.Value(ctypes.c_bool, False)
        self.position = multiprocessing.Value(ctypes.c_longlong, 0)

    def init_reading(self):
        """
        Call from the reading process, initializing local variables
        """
        if not os.path.exists(self.aln_path):
            raise AlignmentException("Can't open {0}".format(self.aln_path))
        self.aln_file = open(self.aln_path, "r")
        self.processed_contigs = set()
        self.cigar_parser = re.compile("[0-9]+[MIDNSHP=X]")

    def is_eof(self):
        return self.eof.value

    def parse_cigar(self, cigar_str, read_str, ctg_name, ctg_pos):
        ctg_str = self.ref_fasta[ctg_name]
        trg_seq = []
        qry_seq = []
        trg_start = ctg_pos - 1
        trg_pos = ctg_pos - 1
        qry_start = 0
        qry_pos = 0

        first = True
        hard_clipped_left = 0
        hard_clipped_right = 0
        for token in self.cigar_parser.findall(cigar_str):
            size, op = int(token[:-1]), token[-1]
            if op == "H":
                if first:
                    qry_start += size
                    hard_clipped_left += size
                else:
                    hard_clipped_right += size
            elif op == "S":
                qry_pos += size
                if first:
                    qry_start += size
            elif op == "M":
                qry_seq.append(read_str[qry_pos : qry_pos + size].upper())
                trg_seq.append(ctg_str[trg_pos : trg_pos + size].upper())
                qry_pos += size
                trg_pos += size
            elif op == "I":
                qry_seq.append(read_str[qry_pos : qry_pos + size].upper())
                trg_seq.append("-" * size)
                qry_pos += size
            elif op == "D":
                qry_seq.append("-" * size)
                trg_seq.append(ctg_str[trg_pos : trg_pos + size].upper())
                trg_pos += size
            else:
                raise AlignmentException("Unsupported CIGAR operation: " + op)
            first = False

        trg_seq = "".join(trg_seq)
        qry_seq = "".join(qry_seq)
        matches = 0
        for i in xrange(len(trg_seq)):
            if trg_seq[i] == qry_seq[i]:
                matches += 1
        err_rate = 1 - float(matches) / len(trg_seq)

        trg_end = trg_pos
        qry_end = qry_pos + hard_clipped_left
        qry_len = qry_end + hard_clipped_right

        return (trg_start, trg_end, len(ctg_str), trg_seq,
                qry_start, qry_end, qry_len, qry_seq, err_rate)

    def get_chunk(self):
        """
        Alignment file is expected to be sorted!
        """
        buffer = []
        parsed_contig = None

        with self.lock:
            self.aln_file.seek(self.position.value)
            if self.eof.value:
                return None, []

            current_contig = None
            while True:
                self.position.value = self.aln_file.tell()
                line = self.aln_file.readline()
                if not line: break
                if line.startswith("@"): continue   #ignore headers

                tokens = line.strip().split()
                if len(tokens) < 11:
                    raise AlignmentException("Error reading SAM file")

                read_contig = tokens[2]
                flags = int(tokens[1])
                is_unmapped = flags & 0x4
                is_secondary = flags & 0x100
                #is_supplementary = flags & 0x800

                if is_unmapped or is_secondary: continue
                if read_contig in self.processed_contigs:
                    raise AlignmentException("Alignment file is not sorted")

                if read_contig != current_contig:
                    prev_contig = current_contig
                    current_contig = read_contig

                    if prev_contig is not None:
                        self.processed_contigs.add(prev_contig)
                        parsed_contig = prev_contig
                        break
                    else:
                        buffer = [tokens]
                else:
                    buffer.append(tokens)

            if not parsed_contig:
                self.eof.value = True
                parsed_contig = current_contig
        #end with

        alignments = []
        for tokens in buffer:
            read_id = tokens[0]
            read_contig = tokens[2]
            cigar_str = tokens[5]
            read_str = tokens[9]
            ctg_pos = int(tokens[3])
            flags = int(tokens[1])
            is_reversed = flags & 0x16

            (trg_start, trg_end, trg_len, trg_seq,
            qry_start, qry_end, qry_len, qry_seq, err_rate) = \
                    self.parse_cigar(cigar_str, read_str, read_contig, ctg_pos)

            if qry_end - qry_start < self.min_aln_length: continue

            aln = Alignment(read_id, read_contig, qry_start,
                            qry_end, "-" if is_reversed else "+",
                            qry_len, trg_start, trg_end, "+", trg_len,
                            qry_seq, trg_seq, err_rate)

            alignments.append(aln)

        return parsed_contig, alignments


def check_binaries():
    if not which(MINIMAP_BIN):
        raise AlignmentException("Minimap2 is not installed")          
    if not which(GRAPHMAP_BIN):
        raise AlignmentException("GraphMap is not installed")
    if not which("sort"):
        raise AlignmentException("UNIX sort utility is not available")


def make_alignment(reference_file, reads_file, num_proc,
                   work_dir, platform, out_alignment, mapping_tool):
    """
    Runs mapper and sort its output
    """
    _run_mapper(reference_file, reads_file, num_proc, platform, out_alignment, mapping_tool)
    logger.debug("Sorting alignment file")
    temp_file = out_alignment + "_sorted"
    env = os.environ.copy()
    env["LC_ALL"] = "C"
    subprocess.check_call(["sort", "-k", "3,3", "-T", work_dir, out_alignment],
                          stdout=open(temp_file, "w"), env=env)
    os.remove(out_alignment)
    os.rename(temp_file, out_alignment)


def get_contigs_info(contigs_file):
    contigs_info = {}
    contigs_fasta = fp.read_fasta_dict(contigs_file)
    for ctg_id, ctg_seq in contigs_fasta.iteritems():
        contig_type = ctg_id.split("_")[0]
        contigs_info[ctg_id] = ContigInfo(ctg_id, len(ctg_seq),
                                          contig_type)

    return contigs_info


def shift_gaps(seq_trg, seq_qry):
    """
    Shifts all ambigious query gaps to the right
    """
    lst_trg, lst_qry = list("$" + seq_trg + "$"), list("$" + seq_qry + "$")
    is_gap = False
    gap_start = 0
    for i in xrange(len(lst_trg)):
        if is_gap and lst_qry[i] != "-":
            is_gap = False
            swap_left = gap_start - 1
            swap_right = i - 1

            while (swap_left > 0 and swap_right >= gap_start and
                   lst_qry[swap_left] == lst_trg[swap_right]):
                lst_qry[swap_left], lst_qry[swap_right] = \
                            lst_qry[swap_right], lst_qry[swap_left]
                swap_left -= 1
                swap_right -= 1

        if not is_gap and lst_qry[i] == "-":
            is_gap = True
            gap_start = i

    return "".join(lst_qry[1 : -1])


def _run_mapper(reference_file, reads_file, num_proc, platform, out_file, mapping_tool):
    if mapping_tool == "minimap2":
        cmdline = [MINIMAP_BIN, reference_file, reads_file, "-a", "-Q",
                   "-w5", "-m100", "-g10000", "--max-chain-skip", "25",
                   "-t", str(num_proc)]
        if platform == "nano":
            cmdline.append("-k15")
        else:
            cmdline.append("-Hk19")
    else:
        cmdline = [GRAPHMAP_BIN, "align", "-r", reference_file, "-d", reads_file,
                   "-t", str(num_proc), "-b", "0", "-o", out_file]
        #FIXME: Find a way to output GraphMap sam without QValues (due to large SAM file this way)
        # "-b 5" does output without QValues, but also headers will contain timing and stuff which breaks ABruijn on another spot

    try:
        devnull = open(os.devnull, "w")
        if mapping_tool == "minimap2":
          subprocess.check_call(cmdline, stderr=devnull, stdout=open(out_file, "w"))          
        else:
          subprocess.check_call(cmdline, stderr=devnull)
    except (subprocess.CalledProcessError, OSError) as e:
        if e.returncode == -9:
            logger.error("Looks like the system ran out of memory")
        raise AlignmentException(str(e))
