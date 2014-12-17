from ast import (fix_missing_locations, iter_fields, parse, Assign, AST, 
                 Attribute, Call, Expr, Index, Load, Name, NodeTransformer, Num, 
                 Return, Store, Str, Subscript, Tuple)
import sys
import traceback

from codetracers.report_builder import ReportBuilder

CONTEXT_NAME = '__live_coding_context__'
RESULT_NAME = '__live_coding_result__'
PSEUDO_FILENAME = '<live coding source>'
MODULE_NAME = '__live_coding__'

class TraceAssignments(NodeTransformer):
    def visit(self, node):
        new_node = super(TraceAssignments, self).visit(node)
        body = getattr(new_node, 'body', None)
        if body is not None:
            previous_line_number = getattr(new_node, 'lineno', None)
            try:
                statements = iter(body)
            except TypeError:
                # body doesn't contain statements
                statements = []
            for statement in statements:
                line_number = getattr(statement, 'lineno', None)
                if (line_number is None and 
                    statement is not None and
                    previous_line_number is not None):
                    statement.lineno = previous_line_number
                else:
                    previous_line_number = line_number
        return new_node
        
    def visit_Call(self, node):
        existing_node = self.generic_visit(node)
        value_node = existing_node.func
        
        names = []
        while isinstance(value_node, Attribute):
            names.insert(0, value_node.attr)
            value_node = value_node.value
        if not names or not hasattr(value_node, 'id'):
            return existing_node
        names.insert(0, value_node.id)
        
        args = [Str(s='.'.join(names[0:-1])),
                Call(func=Name(id='repr', ctx=Load()),
                     args=[existing_node.func.value],
                     keywords=[],
                     starargs=None,
                     kwargs=None),
                existing_node,
                Call(func=Name(id='repr', ctx=Load()),
                     args=[existing_node.func.value],
                     keywords=[],
                     starargs=None,
                     kwargs=None),
                Num(n=existing_node.lineno)]
        new_node = self._create_bare_context_call('record_call', args)
        return new_node

    def visit_Assign(self, node):
        existing_node = self.generic_visit(node)
        new_nodes = [existing_node]
        for target in existing_node.targets:
            trace = self._trace_assignment(target)
            if trace:
                new_nodes.append(trace)

        return new_nodes
    
    def visit_AugAssign(self, node):
        existing_node = self.generic_visit(node)
        new_nodes = [existing_node]
        new_nodes.append(self._trace_assignment(existing_node.target))
        return new_nodes
    
    def _find_line_numbers(self, node, line_numbers):
        """ Populates a set containing all line numbers used by the node and its
        descendants.
        
        line_numbers is a set that all the line numbers will be added to."""
        line_number = getattr(node, 'lineno', None)
        if line_number is not None:
            line_numbers.add(line_number)
        for _, value in iter_fields(node):
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, AST):
                        self._find_line_numbers(item, line_numbers)
            elif isinstance(value, AST):
                self._find_line_numbers(value, line_numbers)
    
    def visit_For(self, node):
        new_node = self.generic_visit(node)
        
        line_numbers = set()
        self._find_line_numbers(new_node, line_numbers)
        new_node.body.insert(0, 
                             self._trace_assignment(new_node.target))
        args = [Num(n=min(line_numbers)),
                Num(n=max(line_numbers))]
        new_node.body.insert(0,
                             self._create_context_call('start_block', args))
        return new_node
    
    def visit_While(self, node):
        new_node = self.generic_visit(node)
        
        line_numbers = set()
        self._find_line_numbers(new_node, line_numbers)
        args = [Num(n=min(line_numbers)),
                Num(n=max(line_numbers))]
        new_node.body.insert(0,
                             self._create_context_call('start_block', args))
        return new_node
    
    def visit_FunctionDef(self, node):
        if node.name == '__repr__':
            return node
        
        new_node = self.generic_visit(node)
        
        line_numbers = set()
        self._find_line_numbers(new_node, line_numbers)
        
        # trace function parameter values
        argument_count = 0
        for target in new_node.args.args:
            if isinstance(target, Name) and target.id == 'self':
                continue
            new_node.body.insert(argument_count, 
                                 self._trace_assignment(target))
            argument_count += 1

        args = [Num(n=min(line_numbers)),
                Num(n=max(line_numbers))]
        new_node.body.insert(0,
                             self._create_context_call('start_block', args))
        return new_node

    def visit_Lambda(self, node):
        """ Instrument a lambda expression by displaying the parameter values.
        
        We create calls to trace assignment to each argument, then wrap them
        all in a tuple together with the original expression, and pull the
        original expression out of the tuple.
        """
        
        new_node = self.generic_visit(node)
        
        line_numbers = set()
        self._find_line_numbers(new_node, line_numbers)
        
        # trace lambda argument values
        calls = [getattr(self._trace_assignment(target), 'value', None)
                 for target in new_node.args.args
                 if getattr(target, 'id', 'self') != 'self']

        args = [Num(n=min(line_numbers)),
                Num(n=max(line_numbers))]
        calls.insert(0, self._create_context_call('start_block', args).value)
        calls.append(new_node.body)
        new_node.body = Subscript(value=Tuple(elts=calls,
                                              ctx=Load()),
                                  slice=Index(value=Num(n=-1)),
                                  ctx=Load())
        return new_node
    
    def visit_Return(self, node):
        existing_node = self.generic_visit(node)
        value = existing_node.value
        if value is None:
            value = Name(id='None', ctx=Load())
        
        return [Assign(targets=[Name(id=RESULT_NAME, ctx=Store())],
                       value=value),
                self._create_context_call('return_value', 
                                          [Name(id=RESULT_NAME, ctx=Load()),
                                           Num(n=existing_node.lineno)]),
                Return(value=Name(id=RESULT_NAME, ctx=Load()))]
    
    def _trace_assignment(self, target):
        #name, value, line number
        if isinstance(target, Name):
            args = [Str(s=target.id), 
                    Name(id=target.id, ctx=Load()),
                    Num(n=target.lineno)]
        elif isinstance(target, Subscript):
            return self._trace_assignment(target.value)
        elif isinstance(target, Attribute):
            args = [Str(s='%s.%s' % (target.value.id, target.attr)),
                    Attribute(value=target.value, 
                              attr=target.attr, 
                              ctx=Load()),
                    Num(n=target.lineno)]
        else:
            return None
            
        return self._create_context_call('assign', args)
        
    def _create_context_call(self, function_name, args):
        return Expr(value=self._create_bare_context_call(function_name, args))
        
    def _create_bare_context_call(self, function_name, args):
        context_name = Name(id=CONTEXT_NAME, ctx=Load())
        function = Attribute(value=context_name,
                             attr=function_name,
                             ctx=Load())
        return Call(func=function,
                    args=args,
                    keywords=[],
                    starargs=None,
                    kwargs=None)

class CodeTracer(object):
    def __init__(self):
        self.message_limit = 1000
        self.keepalive = False
        self.environment = {'__name__': MODULE_NAME}
        
    def trace_code(self, source):
        self.builder = ReportBuilder(self.message_limit)

        try:
            tree = parse(source)
        
            visitor = TraceAssignments()
            new_tree = visitor.visit(tree)
            fix_missing_locations(new_tree)
            
            code = compile(new_tree, PSEUDO_FILENAME, 'exec')
            
            self.environment[CONTEXT_NAME] = self.builder
            exec(code, self.environment)
        except SyntaxError as ex:
            messages = traceback.format_exception_only(type(ex), ex)
            self.builder.add_message(messages[-1].strip() + ' ', ex.lineno)
        except:
            exc_info = sys.exc_info()
            try:
                is_reported = False
                self.builder.message_limit = None # make sure we don't hit limit
                etype, value, tb = exc_info
                messages = traceback.format_exception_only(etype, value)
                message = messages[-1].strip() + ' '
                entries = traceback.extract_tb(tb)
                for filename, line_number, _, _ in entries:
                    if filename == PSEUDO_FILENAME:
                        self.builder.add_message(message, line_number)
                        is_reported = True
                if not is_reported:
                    self.builder.add_message(message, 1)
#                    print '=== Unexpected Exception in tracing code ==='
#                    traceback.print_exception(etype, value, tb)
            finally:
                del tb
                del exc_info # prevents circular reference
                
        return self.builder.report()