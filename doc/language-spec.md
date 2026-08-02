
## Semantic Explanation of Syntax

### Top-Level

A wyrm module is a list of statements. Every statement produces
a result. Any standalone expression is the value of the expression.

The statement rule is valid for all special statements: `fn`,
`class`, `do`, `with`, `import`, `using`.

### Literals

    literal_expr:
      | signed_number
      | strings
      | character
      | 'true'
      | 'false'
      | 'nil'

The value of any literal as a statement is the literal.

#### Signed Numbers

Integers leverage standard C semantics: -?\d+ or 0x\[a-fA-F0-9]+

    0xdead
    12345
    -10

Floating point numbers follow standard C semantics:

    3e1
    3.14159
    -18.24

#### Booleans

Specify true of false

    true # True
    false # False

#### Nil

`nil` is a keyword for a unset variable.

    nil

#### Symbol

A symbol is an identifier / name in Wyrm code. It may be specified
by a single quote preceding a valid wyrm name:

    'name

An implementation may limit significant characters in a symbol. An
implementation must support at least 31 characters of significance.

#### Strings

String literals are generally interned. Wyrm strings are considered
immutable.

Normal strings are simple double-quoted values:

    "this is a string"

Multiline strings may be specified using triple double quote:

    """This is a string
    this is still part of the string."""

Raw strings may be defined using the R prefix with specific token:

    R"(Arbitrary Text)"
    R"extra(WE can noow have (" )extra"

#### Characters

A character literal is Clojure-style: a backslash followed by either a
single character, or one of a handful of named characters. It evaluates to
that character's numeric (u32) value - the same value string indexing
already produces, e.g. `\a == "asdf"[0]`.

    \a
    \newline
    \space
    \tab
    \return
    \backspace
    \formfeed
    \null

### Built-in Collections

The value of any collection as a statement is the collection.

#### Tuples

Tuples denoted by the comma operator (least precedence):

    1, 2, 3, 4

Single element tuple use parens to force the group:

    (1,)

Empty tuple may be specified by open-close parens '()'

#### Pair / List

A list is a sequence of pairs. Wyrm provides the same general
shorthand syntax as Scheme for defining lists, but substitutes
brackets for parens. An improper list may be define using the
'$.' operator and empty list is also allowed:

    []                # empty list, as in scheme '()
    ['a]              # single element, as in scheme cons('a, '())
    [1, 2, 3]         # normal list
    [1, 2, $. 4]      # improper list

#### Arrays

Arrays are defined as a set of variables of a single primitive type,
but that primitive type may be an 'object'. The type of an array
literal is determined by the least generic non-union type qualifying
all items. The '$[' sigil defines the type.

    $[1, 2, 3, 4]


#### Tables or Dictionaries

Dictionary definitions use a `${` sigil rather than bare braces.

    ${ "Name": 15 }

Empty dictionary:

    ${}

### Type Constraints

A type constraint is a specific syntax for requiring types in function
definitions, generics, or type checks.

A type identifier alone may be used as a constraint:

    int
    MyClass

Type identifiers are allowed to have parameters (generic support); the
parameter is a comma separate list of type identifiers. Additionally,
may be a list of parameters:

    list[int]
    callable[[int, float], int]

### Operators

Numerical operators follow same rules as C/C++/Python:

    a ** 2     # Exponents
    a + b + c  # Addition, LTR
    a * b * c  # Multiplication, LTR
    a - b - c  # Subtraction, LTR
    a % b % c  # Modulus, LTR
    a / b / c  # Division, LTR

Bitwise operators:

    a & b
    a | b
    a ^ b

Boolean operators / set operators:

    a or b
    a and b
    not a
    a in b

Type checking, (expression) is (type constraint) -

    a is int            # Simple Type check
    b is int | float    # Check against sum type

Comparisons:

    a <= b
    a >= b
    a <=> b
    a < b
    b > b
    a == b
    a != b

Lookup Operator:

    arr[0]
    dictionary[key]

### Blocks

Wyrm follows Haskell's Layout-Rule. Preferred style is that of Python, braces
are intended to allow one-liners and compressed scripts. Statements may be
terminated by newline, semicolon, or a brace matching the block.

    # If statement
    if foo:
        action()
    another_action()

    # If statement, braces
    if foo { action(); } another_action();

### Variables

Variables defined by simply setting them:

    # Set with type
    foo: int = 5

    # Multivalue - type must be specified before
    foo, bar = 5, 4

A type hint may only be defined for a variable once within a scope.
A variable may be constrained to a type with a type hint statement:

    foo: int

A type hint constraint statement does _not_ define the variable - access
afterward is equivalent to an unset/undefined variable. Undefined
variables will generate an error if attempted to be evaluated:

    if foo:

Generates an error if not assigned. It's possible to test if a variable
is defined in the current scope utilizing the 'defined()' function. The
symbol name must be used:

    if defined('foo):
        foo = foo + 1
    else:
        foo = 1

Wyrm offers a 'set if unset' operator. Evaluation is short circuited
if the variable is already defined and not of error type, otherwise
the evaluation is processed:

    foo ?= 5
    foo_with_type: int ?= 5

A variable may have a type specified prior to assignment:

    foo: int
    if check:
        foo = 5
    else:
        foo = 10

Static variables start with the '$' operator. A static variable is tied
to the symbol definition rather than the execution context. A static
variable should generally be initialized with the `?=` operator.

    fn call_count():
        $foo: int ?= 0
        $foo = $foo + 1
        return $foo

### Modules and Imports

Import is simple:

    import mod

Imports with nested submodules:

    import mod::baz::bar

Once a module is imported, the name scope operator can pull in elements:

    import mod
    mod::function()

The import statement creates the namespace for each module part. It is
valid to alias the import if desired:

    import mod::baz::bar as bar

The alias results in only 'bar' being added to the module namespace.

The 'using' statement allows further manipulation and aliasing of names.

Using an imported module name results in a wild card import of all exported names
within the module:

    import math
    using math

This works with aliased modules as well:

    import math as bar
    using bar

Using may also import individual symbols:

    using sin from math;

Or to create aliases with as keyword:

    using long_math_name_lots_of_typing as corefn from math;

And a list is valid:

    using sin, cos, long_math_name as lfn from math;

Parens are allowed:

    using (sin, cos) from math

### Special Blocks

The with keywords allows binding a series of immutable variables to expression
values. Setting a variable defined using with is considered an error.

    with:
        speed_of_light: float = 299_792_458.0;
        gravitational_constant = 6.6743e-11;

The do keyword allows creation of a scope, the equivalent to defining a lambda
function and immediately calling it. Used in an expression, the value of the
do statement is the last executed line:

    complex_answer = do:
        step_1()
        step_2()
        ...
        step_n()
        10

    # complex_answer == 10

### Basic Functions

Basic functions should look exceedingly familiar to Python users. Most all
 the same rules apply – including no function overloading.

The basic syntax for a function is:

    fn [type...] name(parameters...) -> [result type constraint] block...

Most elements are optional, a minimal function definition with no parameters:

    fn hello():
        pass

Functions return a value with the return keyword statement:

    fn message():
        return "Hello World"

Return type may be specified:

    fn message() -> str:
        return "Hello World"

If no explicit 'return' is used, the value of the last statement is used:

    fn message() -> str:
        "Hello World"

A function may return multiple values:

    fn message() -> int, str, str:
        return 1984, "Text Here", "Text Here 2"

Parameters may be specified:

    fn message(name) -> str:
        return "Hello " + name

    fn message(greeting: str, name: str) -> str:
        return greeting + name
    
Variable length arguments may be collected by the '*' operator:

    fn message(*arguments) -> str:
        greeting, name = arguments
        return greeting + name

Arguments may have default values:

    fn message(name: str, greeting: str = "Hello") -> str:
        return greeting + name

The '/' special argument specifies prior arguments must be positional:

    fn message(name: str, /,  greeting: str = "Hello") -> str:
        return greeting + name

And '**' may be used to collect keyword argument into a dict:

    fn message(**kwargs) -> str:
        return kwargs["greeting"] + kwargs["name"]

### Control Flow

Basic control flow statements - if, while, and for.

    if condition:
        statements
    elif condition_2:
        statements
    elif condition_n:
        statements
    else:
        statements

While statement

    while condition:
        statement
        if condition:
            continue
        if condition_2:
            break

For statement:

    for var in iterable:
        statement
        if condition:
            break
    else:
        statement_if_no_break

Like other statements, `if`/`while`/`for` produce the value of the
last statement executed in whichever branch or iteration actually
ran. If a branch is skipped entirely - an `if` with no matching
`elif`/`else`, or a `while`/`for` whose body never executes - the
value is `nil`.

    fn t1(choice: bool) -> int:
        if choice:
            5
        else:
            10

    # t1(true) -> 5
    # t1(false) -> 10

    fn t2(choice: bool) -> int:
        if choice:
            5

    # t2(true) -> 5
    # t2(false) -> nil          (no else; condition was false)

`break` may carry a value, which becomes the loop's value in place
of whatever statement last executed:

    fn first_even(items) -> int:
        for x in items:
            if x % 2 == 0:
                break x
        else:
            nil

    # returns the first even item, or nil if the loop completes
    # (or is empty) without finding one

Try statement. If the type of the expression is an error, return immediately:

    file = try open('badfile.txt')

The catch statement may be used instead of try to set a value in case of error:

    file = open('badfile.txt') catch open('goodfile.txt')

The try statement may specify the resultant return value using return.

    value = lookup_table['value'] catch return 0

Defer block. The contents of the block are executed when the dynamic
scope of the containing block is complete.

    v = new resource()
    defer:
        v ! release()

Defer with return condition. The block is armed during the dynamic scope
of the calling block and will trigger if any block within the calling block's
dynamic scope forces a return with either 'return' or 'try' statements.

    v = new resource()
    defer on error:       # equivalent to defer { if ( defined_return_value is error ) ... }
        v ! release()

### Messages

A message is a method on a class. Messages may be dispatched utilizing the
message operator `!`.

In simple cases, a message may utilized just as a method call:

    arr_len = array_object!length();

A message may be executed on a tuple:

    (canvas, shape) ! draw();

### Basic Classes

Classes are used to define structure suitable for dynamic dispatch. Objects
work in a similar fashion to CLOS or Dylan. A class is a collection of variables
associated with an inheritance tree.

A basic class defines a data structure and inheritance tree. The class name
may then be used to specify dispatch of methods. A simple class is created
by simple defining the data structure:

    class person:
        slot name: str

    class family_member(person):
        slot relation: str

    class coordinate2d {
        slot x: float;
        slot y: float;
    }

Classes may have methods defined internally:

    class person:
        slot first_name: str = "John"
        slot last_name: str = "Doe"

        fn get_full_name() -> str:
            return last_name + " " + first_name

In the lexical scope of a class, internal memory defined by a
slot definition is accessible using the slot name directly. In
the above example 'last_name' and 'first_name' reference the
internal backing.

Note that the function 'get_full_name' has access to the internal
storage for first_name and last_name. A class method may also be
defined externally:

    fn [person] get_full_name() -> str:
        return this.last_name + " " + this.first_name

Class attributes and variables can be access with the `.` operator.
The `.` operator looks up the given name and creates an attribute
ref. Assigning to the attribute ref results in setting a property.

    person.first_name = "Sam"

Functions defined within the class scope will have attributes
within the created created class. These may be accessed and
called directly, though this is not normally recommended:

    person.get_full_name()

The preferred mechanism to call a method is to utilize the
message operator `!`. The message operator replaces the class
access operator:

    person!get_full_name()

The `!` operator creates a closure. The closure may be elided
in cases where the method is called directly, or it may
be stored:

    name_func = person!get_full_name
    name_func()

Methods may be defined external to the class. These may *not*
be called using attribute reference, but may be called via
the message operator.

    fn [person] get_name():
        return this.name

    fn [coordinate2d] length_squared() {
        return (x**2 + y**2)
    }

The `super` allows classes to call 'up' the inheritance tree.

    class coordinate3d(coordinate2d) {
        slot z: float;
    }

    fn [coordinate3d] length_squared:
        return super() + (z**2)

Objects are constructed with the 'new' keyword. The new keyword specifies
parameters to the constructor:

    person_instance = new person()

The value of the new type is either the object type constructed or error.
Essentially:

    fn person_init() -> person | error { return /* constructed person */; }

A class constructor may be specified within the class. The constructor defines
the default initialized variables for the object and then a block of code
that executes immediately after. All variables must have defined default

Basic syntax:

    init (arguments) [with 'defaults block...'] [do: 'initialization block']

Example with both with and do:

    class vector:
        slot x: float
        slot y: float
        slot len: float
    
        init (a_x: float, a_y: float) with:
            x = a_x
            y = a_y
        do:
            # this.len == 0 (system forced default value)
            this.len = (x ** 2 + y ** 2) ** 0.5

Example with only do:

    class options:
        slot opt_1: int = 0
        slot opt_2: int = 0

        init (set: int, first: bool) do:
            if first:
                this.opt_1 = set
            else:
                this.opt_2 = set

Errors / RAII - returning an error in init overides the 'new' result:

    class demo:
        slot result: int

        init (num: int, den: int) do:
            this.result = try num / den

    x: demo | str = new demo(5, 0) catch 'div0'


#### Slots

The slot keyword defines a data element with the class. Definition of a slot
creates automatic getter/setter functions. The default value for a slot may
be specified:

    class person:
        slot name: str = "John Doe"

The with keyword allows specifying extended options on a slot. The
normal syntax of the with statement applies. Constants set within
the with statement are treated as parameters to the slot creator:

    class person
        slot name: str = "John Doe" with:
            setter = fn (value) { this.name = value; }
            getter = undefined;

The slot creator accepts the following parameters:

    setter: function with parameter for the new value

    getter: function that returns the value

### Coroutines

Coroutines generally follow the exact same syntax of functions. A basic
coroutine assume any/any for input and output.

    co simple():
        yield 1

Output may be specified:

    co count_to_5() -> int:
        yield 1
        yield 2
        yield 3
        yield 4
        yield 5

Coroutines may also accept values:

    co mirror_5x() -> float:
        a = yield 0
        a = yield a
        a = yield a
        a = yield a
        a = yield a
        yield a

The input type may be specified:

    co div2_1x(<- int) -> float:
        a = yield 0
        yield a / 2.0

An example with other parameters:

    co add_5x(<- float, addend: float) -> float:
         a = yield 0
         a = yield a + addend
         a = yield a + addend
         a = yield a + addend
         a = yield a + addend
         yield a + addend


## Types and Type System

### Primitive Types

A wyrm variable reserves place for a primitive. A primitive tags the variable
type and holds a primitive value. The 'is' boolean operator checks that the
variable type matches and the primitive value is identical.

Primitive types:
  - **nil**: a nil value
  - **error**: an error value
  - **bool**: a boolean True/False value
  - **float**: floating point
  - **int**: a machine word
  - **sym**: Symbol table entry (growing dynamic entries)
  - **dict** GC ref: dictionary
  - **pair** GC ref: a pair (or list)
  - **array** GC ref: an array
  - **object** GC ref: object information, any general purpose class or user defined class
  - **str** GC ref: a string value

## Core Features

### Classes

Classes are permitted to perform basic operator overloading. The following
operators may be overloaded: 

    __bool__ - change truthiness of a variable
    __add__ - add two variables
    __sub__ - subtract two variables
    __mul__ - multiply two variables
    __div__ - divide two variables
    __mod__ - modulo two variables
    __pow__ - exponentiation two variables
    __eq__ - equality comparison
    __ne__ - inequality comparison
    __lt__ - less than comparison

### Native Code

**This is an internal feature.**

Wyrm is intended to be a 'self-hosted' language using 'C' as it's internal
assembly language. The special built-in module 'native' specifies the module
is intended for compilation:

import native

Importing native allows a module to create and use C functions. The import module
notifies the interpreter that the module is intended as a compiled wyrm extension.
Attempting to import a module at runtime will result in an error.

The block function defines a native block. It accepts a symbol specifying the
generation portion, a list of input symbols, a list of output symbols, and a
string parameter of content:

    native::block('HEADER, [], [], R"C(

    #include <stdio.h>
    #include <stdlib.h>

    )C")

Block parameters *MUST* be literals. The block function call is detected by the
compiler and used to feed directly into the generated code.

The following portions of a file are available:

    HEADER - includes, headers, definitions
    TYPES - type definitions
    CONSTANTS - constant definitions
    PROTOS - function prototypes
    FUNCTIONS - function definitions

Within a generated function, the block operator may be used to define a chunk of
C code. This chunk of C code will be inserted directly in the generated function.
The code chunk is placed within it's own dedicated scope. The list of input variables
is read, the chunk is placed, and the output variables are written.

    fn quadratic_formula(a, b, c) -> float, float:
        x: float
        y: float
        native::block('HEADER, ['a, 'b, 'c], ['x, 'y], R"C(
            x = (-b + sqrt(b*b - 4*a*c)) / (2*a)
            y = (-b - sqrt(b*b - 4*a*c)) / (2*a)
        )C"
        return x, y

Generated C block:

    wyrm_error w_mymodule_quadratic_formula(wyrm_state* state)
    {
        /* magic start */
        /* magic block start */
        {
           float x;
           float y;
   
           float a = /* magic */;
           float b = /* magic */;
           float c = /* magic */;
   
           x = (-b + sqrt(b*b - 4*a*c)) / (2*a)
           y = (-b - sqrt(b*b - 4*a*c)) / (2*a)
   
           /* magic */ = x;
           /* magic */ = y;
         }
        /* magic block end */
    }

Type Mapping:

 - int -> wyrm_word
 - float -> float
 - bool -> bool
 - str (input only) -> wyrm_string*
 - object -> wyrm_value

## Idiomatic Recommendations

### Error Handling

Annotate functions that may return error using union error:

    fn may_fail() -> int | error:
        ...

Leverage defer on error to handle cleanup:

    resource = new resource()
    defer on error:
        resource ! cleanup()

    try setup(resource)
    return resource

Consider cleaning up and terminating the error if it makes sense:

    resource = new resource()
    defer on error | nil:
        resource ! cleanup()

    try setup(resource)
    check_if_should_return(resource) catch return nil
    return resource

Use the ?= operator to catch and default values. This can be leveraged
and eventually used with a try statement for a series of attempts:

    f = open('try_location_1.txt')
    f ?= open('try_location_2.txt')
    f ?= try open('try_final_location.txt')

Leverage catch to detect actual errors if truthy false is a valid result:

    f = lookup['value'] catch 0
