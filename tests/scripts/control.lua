local total = 0
for i = 1, 10 do
  if i % 2 == 0 then
    total = total + i
  else
    total = total - 1
  end
end
print(total)

local j = 0
while true do
  j = j + 1
  if j > 5 then break end
end
print(j)

for i = 10, 1, -3 do total = total + i end
print(total)

local function classify(x)
  if x < 0 then return "neg" end
  if x == 0 then return "zero" end
  return "pos"
end
print(classify(-5), classify(0), classify(5))

local k = 0
::top::
k = k + 1
if k < 3 then goto top end
print("k", k)

for i = 1, 3 do
  for jj = 1, 3 do
    if jj == 2 then break end
    total = total + i * jj
  end
end
print(total)
