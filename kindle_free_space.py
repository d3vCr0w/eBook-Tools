# A simple script to check the free space on a Kindle device connected via MTP.
# Install libmtp first: brew install libmtp
# Then install pymtp: pip3 install pymtp
import pymtp

mtp = pymtp.MTP()

#BYTES_IN_MB = 1024 * 1024
#BYTES_IN_GB = 1024 * 1024 * 1024
BYTES_IN_MB = 1000 * 1000
BYTES_IN_GB = 1000 * 1000 * 1000

mtp.connect()

total_bytes = mtp.get_totalspace()
free_bytes  = mtp.get_freespace()
used_bytes  = mtp.get_usedspace()

total_gb = total_bytes / BYTES_IN_GB

used_gb  = used_bytes / BYTES_IN_GB
used_mb  = used_bytes / BYTES_IN_MB
used_pct = (used_bytes / total_bytes) * 100

free_gb  = free_bytes / BYTES_IN_GB
free_mb  = free_bytes / BYTES_IN_MB
free_pct = (free_bytes / total_bytes) * 100

print(f"Total : {total_gb:.2f} GB")
print(f"Used  : {used_gb:.2f} GB ({used_mb:,.2f} MB) [{used_pct:.1f}%]")
print(f"Free  : {free_gb:.2f} GB ({free_mb:,.2f} MB) [{free_pct:.1f}%]")

# I choose to leave the device connected, uncomment this to disconnect after checking free space.
#mtp.disconnect()