v {xschem version=3.4.4 file_version=1.2}
G {}
K {}
V {}
S {}
E {}
T {NMOS current mirror 1:2  (Iref=10uA -> Iout=20uA), sky130 nfet_01v8, M1=1u/1u M2=2u/1u, tt corner} 140 -440 0 0 0.3 0.3 {}
N 320 -290 320 -280 {}
N 320 -280 320 -230 {}
N 280 -200 260 -200 {}
N 260 -200 260 -280 {}
N 260 -280 320 -280 {}
N 260 -200 260 -100 {}
N 260 -100 560 -100 {}
N 560 -100 560 -200 {}
N 560 -200 580 -200 {}
N 320 -200 320 -170 {}
N 320 -170 320 -140 {}
N 620 -200 620 -170 {}
N 620 -170 620 -140 {}
N 620 -230 620 -280 {}
N 620 -280 700 -280 {}
C {sky130_fd_pr/nfet_01v8.sym} 300 -200 0 0 {name=M1 L=1 W=1 nf=1 mult=1 model=nfet_01v8 spiceprefix=X}
C {sky130_fd_pr/nfet_01v8.sym} 600 -200 0 0 {name=M2 L=1 W=2 nf=1 mult=1 model=nfet_01v8 spiceprefix=X}
C {devices/isource.sym} 320 -320 0 0 {name=I1 value=10u}
C {devices/vdd.sym} 320 -350 0 0 {name=l1 lab=VDD}
C {devices/gnd.sym} 320 -140 0 0 {name=l2 lab=GND}
C {devices/gnd.sym} 620 -140 0 0 {name=l3 lab=GND}
C {devices/opin.sym} 700 -280 0 0 {name=p1 lab=IOUT}
