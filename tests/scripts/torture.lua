-- multi-value truncation and propagation
local function three() return 1, 2, 3 end
print(three())
print((three()))
print(three(), three())
local a, b, c = three()
print(a, b, c)
local t = { three() }
print(#t, t[1], t[2], t[3])
local t2 = { three(), three() }
print(#t2)

-- calls on call results / literals
local function maker() return function(x) return x * 2 end end
print(maker()(21))
print(("abc"):rep(2), ("%d"):format(7))
print(#"hello", ("x"):byte())

-- assignments and swaps
local p, q = 1, 2
p, q = q, p
print(p, q)
local tbl = {}
tbl.x, tbl.y = 10, 20
print(tbl.x, tbl.y)
tbl[p], tbl[q] = tbl[q], tbl[p]
print(tbl[1], tbl[2])

-- semicolons and empty statements
;;
local z = 1; z = z + 1 ;; print(z)

-- deeply nested tables
local nested = { a = { b = { c = { d = { "deep" } } } } }
print(nested.a.b.c.d[1])

-- closures capturing loop variables
local acc = {}
for i = 1, 3 do
  local j = i * 10
  acc[i] = function() return i + j end
end
print(acc[1](), acc[2](), acc[3]())

-- varargs edge cases
local function pass(...) return ... end
local function sum(...)
  local s = 0
  local args = { ... }
  for i = 1, #args do s = s + args[i] end
  return s
end
print(pass(1, 2, 3))
print(sum(pass(1, 2, 3)))

-- operator torture
local n = 2
print(-n ^ 2, (-n) ^ 2, n ^ -1, 2 * 3 % 4, 2 + 3 * 4 ^ 2)
print(1 < 2 == true, not (1 < 2), 1 .. 2 .. 3)
print(10 // 3, 10 / 2, 10 % 3, -10 // 3, -10 % 3)
print(1 & 3 | 4 ~ 5, 1 << 4, 256 >> 4)

-- long strings inside expressions
local doc = [=[line "one"
line 'two'
line]]three]=]
print(#doc, doc:sub(1, 4))

-- nested function definitions and recursion
local function outer(x)
  local function inner(y) return y + 1 end
  local function rec(k) if k <= 1 then return 1 end return k * rec(k - 1) end
  return inner(x) + rec(x)
end
print(outer(4))

-- table with mixed keys and trailing separators
local mixed = { 1, 2; 3, name = "x"; ["odd key"] = 4, 5, }
print(#mixed, mixed.name, mixed["odd key"])

-- goto in nested blocks
local count = 0
for i = 1, 3 do
  for j = 1, 3 do
    if j == 2 then goto continue end
    count = count + 1
  end
  ::continue::
end
print(count)

-- string comparison and concatenation with numbers
print("a" < "b", "abc" == "abc", "n=" .. 5, "f=" .. 1.5)

-- while with break and nested repeat
local w = 0
while w < 10 do
  w = w + 1
  if w == 5 then break end
end
print(w)

-- deep expression nesting
print(((1 + 2) * (3 + 4)) - ((5 - 6) * (7 - 8)))
