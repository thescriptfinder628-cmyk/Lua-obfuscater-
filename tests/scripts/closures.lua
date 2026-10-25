local function make_counter(start)
  local value = start
  return function(step)
    value = value + (step or 1)
    return value
  end, function() return value end
end

local bump, peek = make_counter(10)
print(bump(), bump(5), peek())

local fns = {}
for i = 1, 3 do
  fns[i] = function() return i * 10 end
end
print(fns[1](), fns[2](), fns[3]())

local outer = 1
local function level1()
  local inner = 2
  local function level2()
    local deepest = 3
    return outer + inner + deepest
  end
  return level2()
end
print(level1())

local function sums(...)
  local total = 0
  for _, v in ipairs({...}) do total = total + v end
  return total, select("#", ...)
end
print(sums(1, 2, 3, 4))

local f = function(a, b) return a - b end
print(f(10, 3))
