P={'shared':0.0000008465,'performance':0.000012732}
INC={'shared':0.25,'performance':2}
R=0.000002316
S=2592000
def price(cpu,typ,ram,mk):
    return (cpu*P[typ]+(ram-cpu*INC[typ])*R)*mk*S
for name,cpu,typ,ram in [('shared-cpu-1x 256MB',1,'shared',0.25),('shared-cpu-1x 1GB',1,'shared',1),('shared-cpu-1x 2GB',1,'shared',2),('shared-cpu-2x 2GB',2,'shared',2),('shared-cpu-2x 4GB',2,'shared',4),('shared-cpu-4x 4GB',4,'shared',4),('performance-1x 2GB',1,'performance',2),('performance-1x 4GB',1,'performance',4),('performance-2x 4GB',2,'performance',4),('performance-2x 8GB',2,'performance',8)]:
    print(f"{name:22s} iad ${price(cpu,typ,ram,1):8.2f}  sin ${price(cpu,typ,ram,1.269230769):8.2f}  sin31d ${price(cpu,typ,ram,1.269230769)*31/30:8.2f}")
print(35.09/price(2,'shared',4,1.269230769))
