local function pack(...)
  return { n = select("#", ...), ... }
end

local p = pack(1, nil, 3)
print(p.n, p[1], p[2] or "nil", p[3])

local function tail(...)
  return ...
end
print(tail(7, 8, 9))

local function count(...)
  local c = 0
  for _ in ipairs({ ... }) do c = c + 1 end
  return c
end
print(count("a", "b"), count())

print(select(2, "x", "y", "z"))
print((select(2, "x", "y", "z")))
