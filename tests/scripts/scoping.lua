-- `local x = x` must read the *outer* x
x = "global"
local x = x
print(x)
do
  local x = "block"
  print(x)
end
print(x)

-- repeat/until: body locals are visible in the condition
local n = 0
repeat
  local done = n >= 3
  local step = 1
  n = n + step
until done
print(n)

-- for loop control variables are per-loop
for i = 1, 2 do
  local i = i * 100
  print(i)
end

local a, b = 1, 2
a, b = b, a
print(a, b)

-- shadowing in nested function scopes
local v = "outer"
local function f()
  local v = "inner"
  return function() return v end
end
print(f()(), v)

-- while body scope
local w = 0
while w < 2 do
  local tmp = w
  w = tmp + 1
end
print("w", w)
